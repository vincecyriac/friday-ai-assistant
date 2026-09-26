"""Gmail and Google Calendar over plain REST.

Gmail is read-and-draft: the scopes granted are readonly and compose, and no
code path here constructs a message-dispatch request. That guarantee is a scope
promise rather than a construction — tests/sentinel/sources/test_guardrails.py
parses this module and fails if any executable string names such an endpoint.

Calendar is guarded: FRIDAY may create events and may only modify the ones it
created, identified by an extended property it stamps on. Modifying a human's
event is refused before any write is attempted.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from email.message import EmailMessage
from typing import Any

from friday.sentinel.sources.base import (AuthExpired, CalendarEvent, MailMessage,
                                          PermissionDenied, SourceError, clean_text,
                                          decode_header_value, strip_html)
from friday.sentinel.sources.oauth import GoogleOAuth

log = logging.getLogger(__name__)

GOOGLE_API = "https://www.googleapis.com"
GMAIL_WEB = "https://mail.google.com/mail/u/0/#inbox"
FRIDAY_STAMP = "friday"
TIMEOUT_S = 20.0
UNREAD_QUERY = "is:unread -in:chats"


def _rfc3339(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_when(when: dict | None) -> tuple[float, bool]:
    """Google gives 'dateTime' for timed events and 'date' for all-day ones."""
    when = when or {}
    if when.get("dateTime"):
        try:
            return dt.datetime.fromisoformat(when["dateTime"]).timestamp(), False
        except ValueError:
            return 0.0, False
    if when.get("date"):
        try:
            day = dt.date.fromisoformat(when["date"][:10])
            return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp(), True
        except ValueError:
            return 0.0, True
    return 0.0, False


def _decode_part(part: dict) -> str:
    data = ((part or {}).get("body") or {}).get("data") or ""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    except Exception:
        return ""


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk_parts(part)


class _GoogleClient:
    """Shared request plumbing. Each capability decides which verbs it offers."""

    def __init__(self, session: Any, oauth: GoogleOAuth, api_base: str) -> None:
        self._session = session
        self._oauth = oauth
        self._base = api_base.rstrip("/")

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None) -> dict:
        token = await self._oauth.token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = f"{self._base}{path}"
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async with self._session.request(method, url, params=params, json=json_body,
                                                 headers=headers) as resp:
                    status = resp.status
                    body = {} if status == 204 else await resp.json(content_type=None)
        except asyncio.TimeoutError as e:
            raise SourceError(f"google {method} timed out") from e
        except (AuthExpired, SourceError):
            raise
        except Exception as e:
            raise SourceError(f"google {method} failed: {type(e).__name__}") from e
        if status in (401, 403):
            raise AuthExpired("google refused the access token; run 'friday-sentinel google-auth'")
        if status >= 300:
            raise SourceError(f"google returned HTTP {status}")
        return body or {}


class GmailAccount(_GoogleClient):
    name = "gmail"

    def __init__(self, session: Any, oauth: GoogleOAuth, *, api_base: str = GOOGLE_API) -> None:
        super().__init__(session, oauth, api_base)

    async def list_unread(self, limit: int = 25) -> list[MailMessage]:
        listing = await self._request("GET", "/gmail/v1/users/me/messages",
                                      params={"q": UNREAD_QUERY, "maxResults": limit})
        out: list[MailMessage] = []
        for stub in listing.get("messages") or []:
            message = await self.fetch(stub.get("id") or "")
            if message is not None:
                out.append(message)
        return out

    async def fetch(self, uid: str) -> MailMessage | None:
        if not uid:
            return None
        raw = await self._request("GET", f"/gmail/v1/users/me/messages/{uid}",
                                  params={"format": "full"})
        if not raw:
            return None
        payload = raw.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in payload.get("headers") or []}
        plain, html = "", ""
        for part in _walk_parts(payload):
            mime = part.get("mimeType") or ""
            if mime == "text/plain" and not plain:
                plain = _decode_part(part)
            elif mime == "text/html" and not html:
                html = _decode_part(part)
        snippet = clean_text(plain) or strip_html(html) or clean_text(raw.get("snippet"))
        try:
            ts = int(raw.get("internalDate") or 0) / 1000.0
        except (TypeError, ValueError):
            ts = 0.0
        return MailMessage(account="gmail", uid=uid,
                           subject=decode_header_value(headers.get("subject")),
                           sender=decode_header_value(headers.get("from")),
                           snippet=snippet, ts=ts, url=f"{GMAIL_WEB}/{uid}",
                           thread_id=raw.get("threadId"))

    async def create_draft(self, to: str, subject: str, body: str,
                           thread_id: str | None = None) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        payload: dict[str, Any] = {"message": {"raw": encoded}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        created = await self._request("POST", "/gmail/v1/users/me/drafts", json_body=payload)
        return clean_text(created.get("id"))


class CalendarGuarded(_GoogleClient):
    name = "calendar"

    def __init__(self, session: Any, oauth: GoogleOAuth, *, api_base: str = GOOGLE_API,
                 calendar_id: str = "primary") -> None:
        super().__init__(session, oauth, api_base)
        self._path = f"/calendar/v3/calendars/{calendar_id}/events"

    async def list(self, time_min: float, time_max: float) -> list[CalendarEvent]:
        body = await self._request("GET", self._path, params={
            "timeMin": _rfc3339(time_min), "timeMax": _rfc3339(time_max),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 50})
        return [self._to_event(raw) for raw in body.get("items") or []]

    async def create(self, summary: str, start: float, end: float,
                     description: str = "") -> CalendarEvent:
        payload = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": _rfc3339(start)},
            "end": {"dateTime": _rfc3339(end)},
            # The stamp is what makes this event modifiable later. Everything
            # without it belongs to a human and is off limits.
            "extendedProperties": {"private": {"created_by": FRIDAY_STAMP}},
        }
        return self._to_event(await self._request("POST", self._path, json_body=payload))

    async def update(self, event_id: str, **fields: Any) -> CalendarEvent:
        await self._require_own(event_id, "update")
        return self._to_event(await self._request("PATCH", f"{self._path}/{event_id}",
                                                  json_body=dict(fields)))

    async def delete(self, event_id: str) -> None:
        await self._require_own(event_id, "delete")
        await self._request("DELETE", f"{self._path}/{event_id}")

    async def in_meeting(self, now: float) -> bool:
        """Timed events only: an all-day 'Conference' must not silence the phone
        for a week."""
        for event in await self.list(now - 3600, now + 3600):
            if not event.all_day and event.start <= now < event.end:
                return True
        return False

    async def _require_own(self, event_id: str, verb: str) -> None:
        raw = await self._request("GET", f"{self._path}/{event_id}")
        if not self._is_ours(raw):
            raise PermissionDenied(
                f"cannot {verb} calendar event {event_id}: FRIDAY did not create it")

    @staticmethod
    def _is_ours(raw: dict) -> bool:
        private = ((raw or {}).get("extendedProperties") or {}).get("private") or {}
        return private.get("created_by") == FRIDAY_STAMP

    def _to_event(self, raw: dict) -> CalendarEvent:
        start, all_day = _parse_when(raw.get("start"))
        end, _ = _parse_when(raw.get("end"))
        return CalendarEvent(
            id=clean_text(raw.get("id")),
            summary=clean_text(raw.get("summary")) or "(no title)",
            organiser=clean_text((raw.get("organizer") or {}).get("displayName")
                                 or (raw.get("organizer") or {}).get("email")),
            start=start, end=end, all_day=all_day,
            created_by_friday=self._is_ours(raw),
            url=clean_text(raw.get("htmlLink")))
