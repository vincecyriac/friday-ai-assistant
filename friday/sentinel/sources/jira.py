"""Jira, read-only by construction.

There is exactly one method that touches the network and it issues GET. Adding
a write would mean adding a method, not changing an argument — which is the
point: a refactor cannot quietly turn a reader into a writer.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from typing import Any

from friday.sentinel.sources.base import AuthExpired, JiraIssue, SourceError, clean_text

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0

# Assigned-and-stuck, plus anything that named me in the last day. Editable in
# the vault: `text ~ currentUser()` is not accepted by every deployment, and the
# first live pass confirms it against the real instance.
DEFAULT_JQL = (
    "((assignee = currentUser() AND statusCategory != Done "
    "AND (priority in (Highest, High) OR status in (Blocked, \"On Hold\") "
    "OR flagged is not EMPTY)) "
    "OR (text ~ currentUser() AND updated >= -1d))"
)


class InvalidQuery(SourceError):
    """Jira rejected the JQL. Retrying the same query cannot help; editing it can."""


def _timestamp(raw: str | None) -> float:
    """Jira's '2026-09-26T09:30:00.000+0000' -> epoch seconds; 0.0 when absent."""
    if not raw:
        return 0.0
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        try:
            return dt.datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0


class JiraReadOnly:
    name = "jira"

    def __init__(self, session: Any, base_url: str, email: str, api_token: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {credentials}", "Accept": "application/json"}

    async def _get(self, path: str, params: dict[str, Any]) -> dict:
        """The only method that opens a socket, and it only ever reads."""
        url = f"{self._base}{path}"
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async with self._session.get(url, params=params, headers=self._headers) as resp:
                    status = resp.status
                    body = await resp.json(content_type=None)
        except asyncio.TimeoutError as e:
            raise SourceError("jira request timed out") from e
        except Exception as e:
            raise SourceError(f"jira request failed: {type(e).__name__}") from e
        if status in (401, 403):
            raise AuthExpired("jira refused the API token; check sources.jira.email and api_token")
        if status == 400:
            messages = (body or {}).get("errorMessages") or ["Jira rejected the query"]
            raise InvalidQuery(f"invalid JQL: {clean_text('; '.join(messages))}")
        if status >= 300:
            raise SourceError(f"jira returned HTTP {status}")
        return body or {}

    async def search(self, jql: str, limit: int = 50) -> list[JiraIssue]:
        body = await self._get("/rest/api/3/search/jql",
                               {"jql": jql, "maxResults": limit,
                                "fields": "summary,priority,status,updated,reporter"})
        issues: list[JiraIssue] = []
        for raw in body.get("issues") or []:
            fields = raw.get("fields") or {}
            key = clean_text(raw.get("key"))
            if not key:
                continue
            issues.append(JiraIssue(
                key=key,
                summary=clean_text(fields.get("summary")),
                who=clean_text((fields.get("reporter") or {}).get("displayName")),
                priority=clean_text((fields.get("priority") or {}).get("name")),
                status=clean_text((fields.get("status") or {}).get("name")),
                updated=_timestamp(fields.get("updated")),
                url=f"{self._base}/browse/{key}",
                is_comment=False,
            ))
        return issues
