"""The shapes every source normalises to, and the text hygiene they all need.

Real mailboxes are not tidy: headers arrive RFC 2047 encoded, most mail is
HTML-only, and a body can be megabytes. Everything here exists so a monitor can
hand the bus one small, clean, JSON-safe item no matter what it was given.
"""

from __future__ import annotations

import html as html_module
import re
import unicodedata
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from typing import Any, Mapping, Protocol, Sequence

SNIPPET_LIMIT = 500
TITLE_LIMIT = 300


class SourceError(RuntimeError):
    """A source failed in a way that is worth retrying."""


class AuthExpired(SourceError):
    """The credential is revoked or expired. Retrying will not help; re-consent will."""


class PermissionDenied(RuntimeError):
    """The capability refused by policy. Nothing is broken, so this is not a SourceError."""


# ------------------------------------------------------------- text hygiene

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BREAKS = re.compile(r"<(br|/p|/div|/tr|/li)\b[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def clean_text(raw: str | bytes | None, limit: int = SNIPPET_LIMIT) -> str:
    """One line of printable text, truncated before anyone stores or ships it."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = unicodedata.normalize("NFC", raw).replace("�", "")
    text = _WHITESPACE.sub(" ", _CONTROL.sub("", text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def decode_header_value(raw: str | None) -> str:
    """RFC 2047 → text. An undecodable header is shown as it arrived, never raised."""
    if not raw:
        return ""
    try:
        return clean_text(str(make_header(decode_header(raw))), limit=TITLE_LIMIT)
    except Exception:
        return clean_text(raw, limit=TITLE_LIMIT)


def strip_html(html: str) -> str:
    """Words without markup. Most real mail has no text/plain part at all."""
    if not html:
        return ""
    body = _SCRIPT_STYLE.sub(" ", html)
    body = _BREAKS.sub(" ", body)
    body = _TAGS.sub("", body)
    return clean_text(html_module.unescape(body))


# ------------------------------------------------------------------- shapes

@dataclass(frozen=True)
class WatchItem:
    """What every source becomes before it reaches the bus."""
    source: str                       # "email" | "calendar" | "jira"
    external_id: str                  # account-qualified for mail: "gmail:<id>" / "imap:<uid>"
    title: str
    snippet: str
    who: str
    url: str
    ts: float
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "external_id": self.external_id, "title": self.title,
                "snippet": self.snippet, "who": self.who, "url": self.url, "ts": self.ts,
                "meta": dict(self.meta)}


@dataclass(frozen=True)
class MailMessage:
    account: str                      # "gmail" | "imap"
    uid: str
    subject: str
    sender: str
    snippet: str
    ts: float
    url: str
    thread_id: str | None


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    organiser: str
    start: float
    end: float
    all_day: bool
    created_by_friday: bool
    url: str


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    who: str
    priority: str
    status: str
    updated: float
    url: str
    is_comment: bool


class MailAccount(Protocol):
    """What a mailbox may do. Note what is absent: nothing sends."""
    name: str

    async def list_unread(self, limit: int = 25) -> Sequence[MailMessage]: ...
    async def fetch(self, uid: str) -> MailMessage | None: ...
    async def create_draft(self, to: str, subject: str, body: str,
                           thread_id: str | None = None) -> str: ...
