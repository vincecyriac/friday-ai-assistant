"""A mailbox over IMAP: read unread, write drafts, and nothing else.

This is the strong guarantee. There is no SMTP host, port or credential in the
settings registry, so there is nothing to dispatch mail with — drafts are written
by APPENDing to the Drafts folder, exactly as a mail client does when you close a
composer without finishing.

imaplib is synchronous, so every call runs in a thread executor under a
timeout: a hung mail server must never block the event loop.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import logging
from email.message import EmailMessage
from typing import Any, Callable

from friday.sentinel.sources.base import (MailMessage, SourceError, clean_text,
                                          decode_header_value, strip_html)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20.0


def _body_snippet(message: email.message.Message) -> str:
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = text
    return clean_text(plain) or strip_html(html)


class ImapAccount:
    name = "imap"

    def __init__(self, host: str, port: int, user: str, password: str, *,
                 drafts_folder: str = "Drafts",
                 connect: Callable[..., Any] = imaplib.IMAP4_SSL,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._drafts = drafts_folder
        self._connect = connect
        self._timeout_s = timeout_s

    # ------------------------------------------------------------- plumbing

    async def _run(self, work: Callable[[Any], Any]) -> Any:
        """One connection per operation, on a worker thread, under a timeout."""
        def session() -> Any:
            client = self._connect(self._host, self._port, timeout=self._timeout_s)
            try:
                client.login(self._user, self._password)
                return work(client)
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, session), self._timeout_s + 5)
        except asyncio.TimeoutError as e:
            raise SourceError(f"imap {self._host} timed out") from e
        except Exception as e:
            # Never let the password reach a log line or an event payload.
            raise SourceError(f"imap {self._host} failed: {type(e).__name__}") from e

    # ---------------------------------------------------------------- reads

    async def list_unread(self, limit: int = 25, since_uid: int = 0) -> list[MailMessage]:
        def work(client: Any) -> list[tuple[str, bytes]]:
            client.select("INBOX", readonly=True)
            criteria = ("UNSEEN",) if not since_uid else (f"{since_uid + 1}:*", "UNSEEN")
            status, data = client.uid("SEARCH", None, *criteria)
            if status != "OK":
                raise SourceError("imap search failed")
            uids = (data[0] or b"").split()
            out = []
            for raw_uid in reversed(uids[-limit:]):              # newest first
                uid = raw_uid.decode()
                status, payload = client.uid("FETCH", raw_uid, "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                out.append((uid, payload[0][1]))
            return out

        return [self._to_message(uid, raw) for uid, raw in await self._run(work)]

    async def fetch(self, uid: str) -> MailMessage | None:
        def work(client: Any):
            client.select("INBOX", readonly=True)
            status, payload = client.uid("FETCH", uid.encode(), "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                return None
            return payload[0][1]

        raw = await self._run(work)
        return None if raw is None else self._to_message(uid, raw)

    # --------------------------------------------------------------- drafts

    async def create_draft(self, to: str, subject: str, body: str,
                           thread_id: str | None = None) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = email.utils.formatdate(localtime=True)
        if thread_id:
            message["In-Reply-To"] = thread_id
            message["References"] = thread_id
        message.set_content(body)
        raw = message.as_bytes()

        def work(client: Any) -> str:
            status, data = client.append(self._drafts, "\\Draft", None, raw)
            if status != "OK":
                raise SourceError(f"imap append to {self._drafts} failed")
            text = (data[0] or b"").decode("utf-8", "replace") if data else ""
            # "[APPENDUID <validity> <uid>] ..." when the server supports UIDPLUS.
            if "APPENDUID" in text:
                try:
                    return text.split("APPENDUID")[1].split("]")[0].split()[1]
                except (IndexError, ValueError):
                    return ""
            return ""

        return await self._run(work)

    # -------------------------------------------------------------- mapping

    def _to_message(self, uid: str, raw: bytes) -> MailMessage:
        parsed = email.message_from_bytes(raw)
        date = parsed.get("Date")
        try:
            ts = email.utils.parsedate_to_datetime(date).timestamp() if date else 0.0
        except (TypeError, ValueError):
            ts = 0.0
        return MailMessage(account="imap", uid=uid,
                           subject=decode_header_value(parsed.get("Subject")),
                           sender=decode_header_value(parsed.get("From")),
                           snippet=_body_snippet(parsed), ts=ts, url="",
                           thread_id=parsed.get("Message-ID"))
