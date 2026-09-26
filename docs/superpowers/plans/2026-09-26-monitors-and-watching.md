# Monitors and Watching (sub-project 4a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the sentinel least-privilege eyes on work mail (Gmail API), personal mail (IMAP), Google Calendar and Jira, normalise everything they see into one `WatchItem` shape on the event bus, deduplicate it in SQLite, and show it in the dashboard — with no scoring, no calls and no way to send mail.

**Architecture:** `friday/sentinel/sources/` holds capability objects that expose only the permitted verbs over raw `aiohttp`/stdlib `imaplib`; `friday/sentinel/watch.py` holds the `Monitor` protocol and the `WatchRunner` the daemon supervises, which reads its switches and intervals from `RuntimeConfig` every tick, writes new items to `watch_items` (the insert *is* the dedupe) and publishes `monitor.item`. A degraded or unauthorised source parks itself without touching the others.

**Tech Stack:** Python ≥ 3.11, `aiohttp` (Google, Jira, and the consent loopback), stdlib `imaplib`/`email` in a thread executor, stdlib `sqlite3`, `ast` for the guardrail tests; vanilla ES modules + Tailwind for the dashboard card; `pytest` + `pytest-aiohttp`.

**Spec:** `docs/superpowers/specs/2026-09-26-monitors-and-watching-design.md` (roadmap: `docs/superpowers/specs/2026-09-21-sentinel-evolution-roadmap.md`, pillar C)

## Global Constraints

- **No commits.** The user's global rule. Leave all work in the working tree on the current branch.
- Python `>=3.11`; no 3.12+ syntax.
- **No new dependencies.** `aiohttp`, `cryptography`, `google-genai`, `psutil`, `python-dotenv` and the stdlib are the whole list. `tests/test_boundaries.py` must stay green after every task.
- **Nothing may send mail.** No `smtplib` import anywhere in `friday/`; no executable string literal in `friday/sentinel/sources/` may contain `send`. Both are enforced by tests.
- Secrets never appear in logs, events, audit detail, error messages or API responses; error text is the HTTP status and reason, never the request.
- Every external call is timeout-bounded; every blocking call (IMAP) runs in a thread executor.
- SQLite stays WAL; migrations forward-only; multi-statement changes use explicit `BEGIN`/`COMMIT`.
- Dashboard: relative URLs only, class strings literal (never concatenated) so `test_css_covers_every_class` can see them, `deploy/build_css.sh` rerun whenever a class changes.
- Use `.venv/bin/python` / `.venv/bin/pytest` for everything.
- Names in a task's **Interfaces → Produces** block are contracts for later tasks; keep them exact.
- **One correction to the spec, applied throughout this plan:** the spec keys `watch_items` on `source:external_id`, but email has *two* accounts and a Gmail message id can equal an IMAP UID. Email items therefore carry an account-qualified id — `external_id = f"{account}:{raw_id}"` where `account` is `gmail` or `imap` — so the row id is e.g. `email:gmail:18f2…`. Calendar and Jira are single-account and unchanged.

## Review Focus

Five things the spec implies, that real accounts will produce on day one, and that no obvious happy-path test would catch. Each has a test in the task that owns the code.

1. **Two mail accounts colliding on an id** — a Gmail id and an IMAP UID that happen to match must produce two rows and two events, not one silently-swallowed message. (Task 6 storage test, Task 7 monitor test.)
2. **HTML-only mail** — most real mail has no `text/plain` part. The snippet must degrade to stripped text rather than being empty or crashing. (Tasks 4 and 5.)
3. **RFC 2047 encoded headers** — `=?UTF-8?B?…?=` subjects and sender names are routine. They must be decoded, never shown raw. (Tasks 4 and 5.)
4. **All-day calendar events** — Google sends `date` instead of `dateTime`, which a naive RFC3339 parse rejects. They must parse, and must not count as "in a meeting". (Task 4.)
5. **Oversized bodies** — a 10 MB mail body or a long Jira description must be truncated before it reaches SQLite or the bus, not after. (Tasks 4, 5 and 6.)

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `friday/sentinel/sources/__init__.py` | re-exports the shapes, errors and capability classes |
| `friday/sentinel/sources/base.py` | `WatchItem`, `MailMessage`, `CalendarEvent`, `JiraIssue`, `SourceError`, `AuthExpired`, `PermissionDenied`, `clean_text`, `decode_header_value` |
| `friday/sentinel/sources/oauth.py` | `GoogleOAuth` — refresh token → cached access token |
| `friday/sentinel/sources/google.py` | `GmailAccount`, `CalendarGuarded` |
| `friday/sentinel/sources/imap.py` | `ImapAccount` |
| `friday/sentinel/sources/jira.py` | `JiraReadOnly` |
| `friday/sentinel/watch.py` | `Monitor`, `WatchContext`, `WatchRunner`, `Sources`, `EmailMonitor`, `CalendarMonitor`, `JiraMonitor` |
| `tests/sentinel/sources/` | `conftest.py` (fake Google/Jira servers, fake imaplib), `test_base.py`, `test_oauth.py`, `test_google.py`, `test_imap.py`, `test_jira.py`, `test_guardrails.py` |
| `tests/sentinel/test_watch.py`, `tests/core/test_storage_v5.py`, `tests/sentinel/test_watch_api.py` | per-task tests |

**Modified**

`friday/core/storage.py`, `friday/sentinel/settings_registry.py`, `friday/sentinel/services.py`, `friday/sentinel/daemon.py`, `friday/sentinel/monitors.py`, `friday/sentinel/cli.py`, `friday/sentinel/web.py`, `friday/sentinel/dashboard/views/overview.js`, `friday/sentinel/dashboard/views/activity.js`, `friday/sentinel/dashboard/tailwind.src.css`, `friday/sentinel/dashboard/tailwind.css`, `tests/core/test_storage.py`, `tests/core/test_storage_v2.py`, `tests/core/test_storage_v3.py`, `tests/core/test_storage_v4.py`, `tests/sentinel/test_settings_registry.py`, `tests/sentinel/test_dashboard_files.py`, `.env.template`, `deploy/README.md`, `readme.md`.

---

### Task 1: Shapes, errors and text hygiene (`sources/base.py`)

**Files:**
- Create: `friday/sentinel/sources/__init__.py`, `friday/sentinel/sources/base.py`, `tests/sentinel/sources/__init__.py` (empty)
- Test: `tests/sentinel/sources/test_base.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) WatchItem(source, external_id, title, snippet, who, url, ts, meta)` with `id` property (`f"{source}:{external_id}"`) and `to_dict()`; `@dataclass(frozen=True) MailMessage(account, uid, subject, sender, snippet, ts, url, thread_id)`; `@dataclass(frozen=True) CalendarEvent(id, summary, organiser, start, end, all_day, created_by_friday, url)`; `@dataclass(frozen=True) JiraIssue(key, summary, who, priority, status, updated, url, is_comment)`; `SourceError`, `AuthExpired(SourceError)`, `PermissionDenied`; `SNIPPET_LIMIT = 500`; `clean_text(raw: str | bytes | None, limit: int = SNIPPET_LIMIT) -> str`; `decode_header_value(raw: str | None) -> str`; `strip_html(html: str) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/sources/test_base.py`:
```python
import dataclasses

import pytest

from friday.sentinel.sources import (AuthExpired, CalendarEvent, JiraIssue, MailMessage,
                                     PermissionDenied, SourceError, WatchItem, clean_text,
                                     decode_header_value, strip_html)
from friday.sentinel.sources.base import SNIPPET_LIMIT


def _item(**kw):
    base = dict(source="email", external_id="gmail:abc", title="Subject", snippet="body",
                who="Ada <ada@example.com>", url="https://mail.example/1", ts=1.0, meta={})
    return WatchItem(**{**base, **kw})


def test_watch_item_id_is_source_qualified_and_serialises():
    item = _item()
    assert item.id == "email:gmail:abc"
    payload = item.to_dict()
    assert payload["source"] == "email" and payload["external_id"] == "gmail:abc"
    assert payload["meta"] == {} and payload["ts"] == 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.title = "no"


def test_two_accounts_with_the_same_raw_id_are_different_items():
    """A Gmail id and an IMAP UID can both be '12345'; they must not collide."""
    assert _item(external_id="gmail:12345").id != _item(external_id="imap:12345").id


def test_errors_form_the_expected_hierarchy():
    assert issubclass(AuthExpired, SourceError)
    assert not issubclass(PermissionDenied, SourceError)      # a refusal is not a failure


@pytest.mark.parametrize("raw,expected", [
    ("  hello   world \n", "hello world"),
    (b"bytes in", "bytes in"),
    (None, ""),
    ("", ""),
    ("tabs\tand\r\nnewlines", "tabs and newlines"),
    ("\x00control\x07chars", "controlchars"),
])
def test_clean_text_normalises(raw, expected):
    assert clean_text(raw) == expected


def test_clean_text_truncates_before_anything_downstream_sees_it():
    out = clean_text("x" * 10_000)
    assert len(out) == SNIPPET_LIMIT and out.endswith("…")
    assert clean_text("y" * 20, limit=10) == "yyyyyyyyy…"


def test_clean_text_survives_undecodable_bytes():
    assert clean_text(b"caf\xe9 noir") == "caf noir" or clean_text(b"caf\xe9 noir") == "café noir"


@pytest.mark.parametrize("raw,expected", [
    ("=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=", "Schöne Grüße"),
    ("=?utf-8?q?Re=3A_status?=", "Re: status"),
    ("plain subject", "plain subject"),
    (None, ""),
    ("=?BOGUS?X?zz?=", "=?BOGUS?X?zz?="),          # undecodable: shown as-is, never crashes
])
def test_decode_header_value(raw, expected):
    assert decode_header_value(raw) == expected


def test_strip_html_keeps_the_words_and_drops_the_markup():
    html = "<html><head><style>p{color:red}</style></head><body><p>Hello <b>you</b></p>" \
           "<script>evil()</script><br>Second line</body></html>"
    out = strip_html(html)
    assert "Hello you" in out and "Second line" in out
    assert "<" not in out and "evil()" not in out and "color:red" not in out
    assert strip_html("") == ""


def test_strip_html_unescapes_entities():
    assert strip_html("<p>A &amp; B &lt;tag&gt;&nbsp;end</p>") == "A & B <tag> end"


def test_source_dataclasses_are_frozen_value_objects():
    m = MailMessage(account="gmail", uid="1", subject="s", sender="a", snippet="x", ts=1.0,
                    url="", thread_id=None)
    c = CalendarEvent(id="e", summary="s", organiser="o", start=1.0, end=2.0, all_day=False,
                      created_by_friday=True, url="")
    j = JiraIssue(key="K-1", summary="s", who="w", priority="High", status="Blocked",
                  updated=1.0, url="", is_comment=False)
    assert (m.account, c.all_day, j.key) == ("gmail", False, "K-1")
    for obj in (m, c, j):
        with pytest.raises(dataclasses.FrozenInstanceError):
            obj.summary = "no" if not isinstance(obj, MailMessage) else "no"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/sources/test_base.py -q`
Expected: `ModuleNotFoundError: No module named 'friday.sentinel.sources'`.

- [ ] **Step 3: Implement**

`friday/sentinel/sources/base.py`:
```python
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
```

`friday/sentinel/sources/__init__.py`:
```python
"""Least-privilege views onto the surfaces FRIDAY watches.

Each capability exposes only the verbs it is allowed to use. There is no
general-purpose request method on any of them, and nothing anywhere can send
mail: the IMAP account holds no SMTP credential (structural) and the Gmail
module contains no send path (enforced by an AST test).
"""

from friday.sentinel.sources.base import (AuthExpired, CalendarEvent, JiraIssue, MailAccount,
                                          MailMessage, PermissionDenied, SourceError, WatchItem,
                                          clean_text, decode_header_value, strip_html)

__all__ = ["AuthExpired", "CalendarEvent", "JiraIssue", "MailAccount", "MailMessage",
           "PermissionDenied", "SourceError", "WatchItem", "clean_text", "decode_header_value",
           "strip_html"]
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/sources -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 2: `GoogleOAuth` (`sources/oauth.py`)

**Files:**
- Create: `friday/sentinel/sources/oauth.py`, `tests/sentinel/sources/conftest.py`
- Modify: `friday/sentinel/sources/__init__.py`
- Test: `tests/sentinel/sources/test_oauth.py`

**Interfaces:**
- Consumes: `AuthExpired`, `SourceError` (Task 1).
- Produces: `GoogleOAuth(session, client_id, client_secret, refresh_token, *, token_url=TOKEN_URL, clock=time.monotonic)` with `async token() -> str`, `credentials_key` property, and `SCOPES: tuple[str, ...]`; `TOKEN_URL = "https://oauth2.googleapis.com/token"`.
- Produces in `tests/sentinel/sources/conftest.py`: `google_server` fixture (an `aiohttp` app standing in for Google's token, Gmail and Calendar endpoints) exposing `.url`, `.calls`, `.token_responses`, `.gmail`, `.calendar`; `jira_server` fixture with `.url`, `.calls`, `.issues`, `.status`; helper `fixed_clock()`.

- [ ] **Step 1: Write the shared fake servers**

`tests/sentinel/sources/conftest.py`:
```python
"""Fake Google and Jira, so no test needs a credential or a network.

Each fake records every request it receives, which is how the allowlist tests
prove a capability never issued a verb it was not supposed to.
"""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web


class RecordingServer:
    def __init__(self):
        self.calls: list[dict] = []
        self.url = ""

    def record(self, request, body=None):
        self.calls.append({"method": request.method, "path": request.path,
                           "query": dict(request.query), "body": body,
                           "headers": dict(request.headers)})

    @property
    def methods(self) -> set[str]:
        return {c["method"] for c in self.calls}


@pytest.fixture
async def google_server(aiohttp_server):
    """Token endpoint + the Gmail and Calendar routes the capabilities use."""
    state = RecordingServer()
    state.token_responses = [{"access_token": "tok-1", "expires_in": 3600}]
    state.gmail = {"messages": [], "byid": {}, "drafts": []}
    state.calendar = {"events": [], "byid": {}, "created": []}
    state.status = {}                       # path suffix -> status to force

    async def token(request):
        body = await request.post()
        state.record(request, dict(body))
        forced = state.status.get("token")
        if forced:
            return web.json_response({"error": "invalid_grant"}, status=forced)
        payload = state.token_responses.pop(0) if len(state.token_responses) > 1 \
            else state.token_responses[0]
        return web.json_response(payload)

    async def messages_list(request):
        state.record(request)
        if state.status.get("gmail"):
            return web.json_response({"error": {"message": "nope"}}, status=state.status["gmail"])
        return web.json_response({"messages": [{"id": m["id"]} for m in state.gmail["messages"]]})

    async def message_get(request):
        state.record(request)
        message = state.gmail["byid"].get(request.match_info["id"])
        if message is None:
            return web.json_response({"error": {"message": "not found"}}, status=404)
        return web.json_response(message)

    async def drafts_create(request):
        state.record(request, await request.json())
        state.gmail["drafts"].append(await request.json() if False else None)
        return web.json_response({"id": "draft-1"})

    async def events_list(request):
        state.record(request)
        if state.status.get("calendar"):
            return web.json_response({"error": {"message": "nope"}}, status=state.status["calendar"])
        return web.json_response({"items": state.calendar["events"]})

    async def event_get(request):
        state.record(request)
        event = state.calendar["byid"].get(request.match_info["id"])
        if event is None:
            return web.json_response({"error": {"message": "not found"}}, status=404)
        return web.json_response(event)

    async def event_create(request):
        body = await request.json()
        state.record(request, body)
        state.calendar["created"].append(body)
        return web.json_response({**body, "id": "new-event"})

    async def event_patch(request):
        body = await request.json()
        state.record(request, body)
        return web.json_response({**body, "id": request.match_info["id"]})

    async def event_delete(request):
        state.record(request)
        return web.Response(status=204)

    app = web.Application()
    app.add_routes([
        web.post("/token", token),
        web.get("/gmail/v1/users/me/messages", messages_list),
        web.get("/gmail/v1/users/me/messages/{id}", message_get),
        web.post("/gmail/v1/users/me/drafts", drafts_create),
        web.get("/calendar/v3/calendars/primary/events", events_list),
        web.get("/calendar/v3/calendars/primary/events/{id}", event_get),
        web.post("/calendar/v3/calendars/primary/events", event_create),
        web.patch("/calendar/v3/calendars/primary/events/{id}", event_patch),
        web.delete("/calendar/v3/calendars/primary/events/{id}", event_delete),
    ])
    server = await aiohttp_server(app)
    state.url = str(server.make_url("")).rstrip("/")
    return state


@pytest.fixture
async def jira_server(aiohttp_server):
    state = RecordingServer()
    state.issues = []
    state.status = 0
    state.error_body = {"errorMessages": ["Error in the JQL Query: unexpected token 'foo'"]}

    async def search(request):
        state.record(request)
        if state.status:
            return web.json_response(state.error_body, status=state.status)
        return web.json_response({"issues": state.issues})

    async def catch_all(request):
        state.record(request)
        return web.json_response({}, status=405)

    app = web.Application()
    app.add_routes([
        web.get("/rest/api/3/search/jql", search),
        web.route("*", "/{tail:.*}", catch_all),
    ])
    server = await aiohttp_server(app)
    state.url = str(server.make_url("")).rstrip("/")
    return state


def fixed_clock(start: float = 0.0):
    """A monotonic clock the test advances by hand."""
    box = SimpleNamespace(now=start)
    box.tick = lambda seconds: setattr(box, "now", box.now + seconds)
    box.read = lambda: box.now
    return box
```

- [ ] **Step 2: Write the failing tests**

`tests/sentinel/sources/test_oauth.py`:
```python
import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, SourceError
from friday.sentinel.sources.oauth import SCOPES, GoogleOAuth
from tests.sentinel.sources.conftest import fixed_clock


async def _oauth(server, clock=None, **kw):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh-1",
                        token_url=f"{server.url}/token",
                        clock=(clock.read if clock else None) or (lambda: 0.0), **kw)
    return session, oauth


async def test_scopes_are_read_and_compose_only():
    assert SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/gmail.compose",
                      "https://www.googleapis.com/auth/calendar.events")
    assert not any("gmail.send" in s or s.endswith("/gmail.modify") for s in SCOPES)


async def test_token_is_fetched_once_and_cached(google_server):
    clock = fixed_clock()
    session, oauth = await _oauth(google_server, clock)
    try:
        assert await oauth.token() == "tok-1"
        assert await oauth.token() == "tok-1"
        assert len(google_server.calls) == 1              # cached, not refetched
        body = google_server.calls[0]["body"]
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "refresh-1"
        assert body["client_id"] == "cid" and body["client_secret"] == "secret"
    finally:
        await session.close()


async def test_token_is_refreshed_before_it_expires(google_server):
    clock = fixed_clock()
    google_server.token_responses = [{"access_token": "tok-1", "expires_in": 3600},
                                     {"access_token": "tok-2", "expires_in": 3600}]
    session, oauth = await _oauth(google_server, clock)
    try:
        assert await oauth.token() == "tok-1"
        clock.tick(3500)                                  # inside the 60 s safety margin
        assert await oauth.token() == "tok-2"
        assert len(google_server.calls) == 2
    finally:
        await session.close()


async def test_invalid_grant_is_auth_expired_not_a_retry(google_server):
    google_server.status["token"] = 400
    session, oauth = await _oauth(google_server)
    try:
        with pytest.raises(AuthExpired):
            await oauth.token()
    finally:
        await session.close()


async def test_a_server_error_is_a_transient_source_error(google_server):
    google_server.status["token"] = 503
    session, oauth = await _oauth(google_server)
    try:
        with pytest.raises(SourceError) as excinfo:
            await oauth.token()
        assert not isinstance(excinfo.value, AuthExpired)
        assert "secret" not in str(excinfo.value) and "refresh-1" not in str(excinfo.value)
    finally:
        await session.close()


async def test_credentials_key_changes_when_the_token_does(google_server):
    session, oauth = await _oauth(google_server)
    try:
        other = GoogleOAuth(session, "cid", "secret", "refresh-2", token_url=f"{google_server.url}/token")
        assert oauth.credentials_key != other.credentials_key
    finally:
        await session.close()
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/sources/test_oauth.py -q`
Expected: `ModuleNotFoundError: friday.sentinel.sources.oauth`.

- [ ] **Step 4: Implement**

`friday/sentinel/sources/oauth.py`:
```python
"""Google OAuth: a refresh token in, a short-lived access token out.

Only the scopes FRIDAY needs are ever requested. `gmail.compose` is granted now
rather than later so that sub-project 4b's draft replies need no second consent
— the protection against sending is that no send path exists, which an AST test
enforces.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable

from friday.sentinel.sources.base import AuthExpired, SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
)
REFRESH_MARGIN_S = 60.0
TIMEOUT_S = 20.0


class GoogleOAuth:
    def __init__(self, session: Any, client_id: str, client_secret: str, refresh_token: str, *,
                 token_url: str = TOKEN_URL, clock: Callable[[], float] | None = None) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_url = token_url
        self._clock = clock or time.monotonic
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def credentials_key(self) -> str:
        """Identifies this credential set without revealing it — used to decide
        whether a cached client may be reused after a vault edit."""
        material = f"{self._client_id}:{self._refresh_token}".encode()
        return hashlib.sha256(material).hexdigest()[:16]

    async def token(self) -> str:
        async with self._lock:
            if self._access is not None and self._clock() < self._expires_at - REFRESH_MARGIN_S:
                return self._access
            payload = {"grant_type": "refresh_token", "refresh_token": self._refresh_token,
                       "client_id": self._client_id, "client_secret": self._client_secret}
            try:
                async with asyncio.timeout(TIMEOUT_S):
                    async with self._session.post(self._token_url, data=payload) as resp:
                        status, body = resp.status, await resp.json(content_type=None)
            except asyncio.TimeoutError as e:
                raise SourceError("google token request timed out") from e
            except Exception as e:                       # network-level failure
                raise SourceError(f"google token request failed: {type(e).__name__}") from e
            if status == 400 or status == 401:
                # invalid_grant: the refresh token is revoked or expired. Retrying
                # cannot fix it; only re-consent can.
                raise AuthExpired("google refused the refresh token; run 'friday-sentinel google-auth'")
            if status >= 300:
                raise SourceError(f"google token request returned HTTP {status}")
            access = (body or {}).get("access_token")
            if not access:
                raise SourceError("google token response contained no access_token")
            self._access = access
            self._expires_at = self._clock() + float((body or {}).get("expires_in") or 3600)
            return access
```

Extend `friday/sentinel/sources/__init__.py`:
```python
from friday.sentinel.sources.oauth import SCOPES, GoogleOAuth
```
and add `"GoogleOAuth"`, `"SCOPES"` to `__all__`.

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/sources -q`
Expected: all PASS.

---

### Task 3: `JiraReadOnly` (`sources/jira.py`)

**Files:**
- Create: `friday/sentinel/sources/jira.py`
- Modify: `friday/sentinel/sources/__init__.py`
- Test: `tests/sentinel/sources/test_jira.py`

**Interfaces:**
- Consumes: `JiraIssue`, `SourceError`, `AuthExpired`, `clean_text` (Task 1); the `jira_server` fixture (Task 2).
- Produces: `JiraReadOnly(session, base_url, email, api_token)` with `async search(jql: str, limit: int = 50) -> list[JiraIssue]`; `DEFAULT_JQL: str`; `InvalidQuery(SourceError)`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/sources/test_jira.py`:
```python
import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, SourceError
from friday.sentinel.sources.jira import DEFAULT_JQL, InvalidQuery, JiraReadOnly


def _issue(key="OPS-1", summary="Disk full", priority="Highest", status="Blocked",
           updated="2026-09-26T09:30:00.000+0000", reporter="Ada Lovelace"):
    return {"key": key, "fields": {"summary": summary,
                                   "priority": {"name": priority},
                                   "status": {"name": status},
                                   "updated": updated,
                                   "reporter": {"displayName": reporter}}}


async def _client(server):
    session = aiohttp.ClientSession()
    return session, JiraReadOnly(session, server.url, "vince@example.com", "token-1")


async def test_search_maps_issues_and_sends_basic_auth(jira_server):
    jira_server.issues = [_issue(), _issue(key="OPS-2", summary="Certificate expiring",
                                           priority="High", status="On Hold")]
    session, jira = await _client(jira_server)
    try:
        issues = await jira.search("assignee = currentUser()", limit=10)
    finally:
        await session.close()

    assert [i.key for i in issues] == ["OPS-1", "OPS-2"]
    first = issues[0]
    assert first.summary == "Disk full" and first.priority == "Highest" and first.status == "Blocked"
    assert first.who == "Ada Lovelace" and first.is_comment is False
    assert first.url == f"{jira_server.url}/browse/OPS-1"
    assert first.updated > 0
    call = jira_server.calls[0]
    assert call["method"] == "GET" and call["query"]["jql"] == "assignee = currentUser()"
    assert call["query"]["maxResults"] == "10"
    assert call["headers"]["Authorization"].startswith("Basic ")


async def test_the_class_exposes_no_write_verb():
    """Structural, not a promise: adding a write means adding a method."""
    import inspect
    source = inspect.getsource(JiraReadOnly)
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source, f"JiraReadOnly must not {verb}"
    assert not any(name.startswith("_post") or name.startswith("_put") or name.startswith("_delete")
                   for name in dir(JiraReadOnly))


async def test_only_get_ever_reaches_the_server(jira_server):
    jira_server.issues = [_issue()]
    session, jira = await _client(jira_server)
    try:
        await jira.search(DEFAULT_JQL)
    finally:
        await session.close()
    assert jira_server.methods == {"GET"}


async def test_a_bad_jql_is_invalid_query_not_a_retry(jira_server):
    jira_server.status = 400
    session, jira = await _client(jira_server)
    try:
        with pytest.raises(InvalidQuery) as excinfo:
            await jira.search("this is not jql")
    finally:
        await session.close()
    assert issubclass(InvalidQuery, SourceError)
    assert "unexpected token" in str(excinfo.value)          # Atlassian's own words, for the UI
    assert "token-1" not in str(excinfo.value)


@pytest.mark.parametrize("status,expected", [(401, AuthExpired), (403, AuthExpired),
                                             (500, SourceError), (502, SourceError)])
async def test_status_mapping(jira_server, status, expected):
    jira_server.status = status
    session, jira = await _client(jira_server)
    try:
        with pytest.raises(expected):
            await jira.search(DEFAULT_JQL)
    finally:
        await session.close()


async def test_a_malformed_issue_is_skipped_not_fatal(jira_server):
    jira_server.issues = [{"key": "OPS-9"}, _issue(key="OPS-10")]     # no fields at all
    session, jira = await _client(jira_server)
    try:
        issues = await jira.search(DEFAULT_JQL)
    finally:
        await session.close()
    assert [i.key for i in issues] == ["OPS-9", "OPS-10"]
    assert issues[0].summary == "" and issues[0].priority == "" and issues[0].updated == 0.0


async def test_default_jql_covers_assigned_blocked_flagged_and_mentions():
    for fragment in ("assignee = currentUser()", "statusCategory != Done", "Highest",
                     "Blocked", "On Hold", "flagged is not EMPTY", "updated >= -1d"):
        assert fragment in DEFAULT_JQL
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/sources/test_jira.py -q`
Expected: `ModuleNotFoundError: friday.sentinel.sources.jira`.

- [ ] **Step 3: Implement**

`friday/sentinel/sources/jira.py`:
```python
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

from friday.sentinel.sources.base import (AuthExpired, JiraIssue, SourceError, clean_text,
                                          decode_header_value)

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
    """Jira's '2026-09-26T09:30:00.000+0000' → epoch seconds; 0.0 when absent."""
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
```

Extend `friday/sentinel/sources/__init__.py` with
`from friday.sentinel.sources.jira import DEFAULT_JQL, InvalidQuery, JiraReadOnly` and add the
three names to `__all__`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/sources -q`
Expected: all PASS.

---

### Task 4: `GmailAccount` and `CalendarGuarded` (`sources/google.py`)

**Files:**
- Create: `friday/sentinel/sources/google.py`
- Modify: `friday/sentinel/sources/__init__.py`
- Test: `tests/sentinel/sources/test_google.py`

**Interfaces:**
- Consumes: `GoogleOAuth` (Task 2), the shapes and text helpers (Task 1), `google_server` (Task 2).
- Produces: `GmailAccount(session, oauth, *, api_base=GOOGLE_API)` with `name = "gmail"`, `async list_unread(limit=25) -> list[MailMessage]`, `async fetch(uid) -> MailMessage | None`, `async create_draft(to, subject, body, thread_id=None) -> str`; `CalendarGuarded(session, oauth, *, api_base=GOOGLE_API, calendar_id="primary")` with `async list(time_min, time_max) -> list[CalendarEvent]`, `async create(summary, start, end, description="") -> CalendarEvent`, `async update(event_id, **fields) -> CalendarEvent`, `async delete(event_id) -> None`, `async in_meeting(now) -> bool`; `GOOGLE_API = "https://www.googleapis.com"`; `FRIDAY_STAMP = "friday"`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/sources/test_google.py`:
```python
import base64
import time

import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, PermissionDenied, SourceError
from friday.sentinel.sources.google import FRIDAY_STAMP, CalendarGuarded, GmailAccount
from friday.sentinel.sources.oauth import GoogleOAuth


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def gmail_message(id="m1", subject="Status update", sender="Ada <ada@example.com>",
                  body_plain="the plain body", body_html=None, internal_ms=1_700_000_000_000,
                  thread="t1"):
    parts = []
    if body_plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(body_plain)}})
    if body_html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(body_html)}})
    return {"id": id, "threadId": thread, "internalDate": str(internal_ms),
            "snippet": "api snippet",
            "payload": {"headers": [{"name": "Subject", "value": subject},
                                    {"name": "From", "value": sender}],
                        "parts": parts}}


async def _gmail(server):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh", token_url=f"{server.url}/token")
    return session, GmailAccount(session, oauth, api_base=server.url)


async def _calendar(server):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh", token_url=f"{server.url}/token")
    return session, CalendarGuarded(session, oauth, api_base=server.url)


# -------------------------------------------------------------------- gmail

async def test_list_unread_maps_messages(google_server):
    google_server.gmail["messages"] = [{"id": "m1"}]
    google_server.gmail["byid"] = {"m1": gmail_message()}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread(limit=5)
    finally:
        await session.close()

    assert len(messages) == 1
    m = messages[0]
    assert m.account == "gmail" and m.uid == "m1" and m.subject == "Status update"
    assert m.sender == "Ada <ada@example.com>" and m.snippet == "the plain body"
    assert m.thread_id == "t1" and m.ts == 1_700_000_000.0
    assert m.url == "https://mail.google.com/mail/u/0/#inbox/m1"
    listing = [c for c in google_server.calls if c["path"].endswith("/messages")][0]
    assert listing["query"]["q"] == "is:unread -in:chats" and listing["query"]["maxResults"] == "5"


async def test_html_only_mail_still_yields_a_snippet(google_server):
    """Most real mail has no text/plain part at all."""
    google_server.gmail["messages"] = [{"id": "m2"}]
    google_server.gmail["byid"] = {"m2": gmail_message(
        id="m2", body_plain=None, body_html="<p>Hello <b>there</b></p><script>x()</script>")}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].snippet == "Hello there"


async def test_a_message_with_no_body_falls_back_to_the_api_snippet(google_server):
    google_server.gmail["messages"] = [{"id": "m3"}]
    google_server.gmail["byid"] = {"m3": gmail_message(id="m3", body_plain=None)}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].snippet == "api snippet"


async def test_encoded_headers_are_decoded(google_server):
    google_server.gmail["messages"] = [{"id": "m4"}]
    google_server.gmail["byid"] = {"m4": gmail_message(
        id="m4", subject="=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=",
        sender="=?utf-8?q?Ada_Lovelace?= <ada@example.com>")}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].subject == "Schöne Grüße"
    assert "Ada Lovelace" in messages[0].sender and "=?utf" not in messages[0].sender


async def test_a_huge_body_is_truncated_before_it_leaves_the_capability(google_server):
    google_server.gmail["messages"] = [{"id": "m5"}]
    google_server.gmail["byid"] = {"m5": gmail_message(id="m5", body_plain="x" * 2_000_000)}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert len(messages[0].snippet) <= 500


async def test_create_draft_posts_a_draft_and_returns_its_id(google_server):
    session, gmail = await _gmail(google_server)
    try:
        draft_id = await gmail.create_draft("ada@example.com", "Re: status", "On it.", thread_id="t1")
    finally:
        await session.close()
    assert draft_id == "draft-1"
    post = [c for c in google_server.calls if c["method"] == "POST" and "drafts" in c["path"]][0]
    raw = post["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "===").decode()
    assert "To: ada@example.com" in decoded and "Subject: Re: status" in decoded
    assert "On it." in decoded and post["body"]["message"]["threadId"] == "t1"


@pytest.mark.parametrize("status,expected", [(401, AuthExpired), (403, AuthExpired),
                                             (500, SourceError)])
async def test_gmail_status_mapping(google_server, status, expected):
    google_server.status["gmail"] = status
    session, gmail = await _gmail(google_server)
    try:
        with pytest.raises(expected):
            await gmail.list_unread()
    finally:
        await session.close()


# ----------------------------------------------------------------- calendar

def gcal_event(id="e1", summary="Standup", start="2026-09-26T09:00:00+02:00",
               end="2026-09-26T09:15:00+02:00", all_day=False, stamped=False,
               organiser="Ada Lovelace"):
    when = ({"date": start[:10]} if all_day else {"dateTime": start})
    until = ({"date": end[:10]} if all_day else {"dateTime": end})
    event = {"id": id, "summary": summary, "start": when, "end": until,
             "organizer": {"displayName": organiser},
             "htmlLink": f"https://calendar.google.com/event?eid={id}"}
    if stamped:
        event["extendedProperties"] = {"private": {"created_by": FRIDAY_STAMP}}
    return event


async def test_list_maps_events_including_all_day(google_server):
    google_server.calendar["events"] = [gcal_event(),
                                        gcal_event(id="e2", summary="Conference", all_day=True)]
    session, cal = await _calendar(google_server)
    try:
        events = await cal.list(0.0, 1e10)
    finally:
        await session.close()
    assert [e.summary for e in events] == ["Standup", "Conference"]
    assert events[0].all_day is False and events[0].start > 0 and events[0].end > events[0].start
    assert events[1].all_day is True and events[1].start > 0      # 'date', not 'dateTime'
    assert events[0].created_by_friday is False


async def test_create_stamps_the_event_as_friday_made(google_server):
    session, cal = await _calendar(google_server)
    try:
        await cal.create("Reminder", 1_700_000_000.0, 1_700_003_600.0, description="from FRIDAY")
    finally:
        await session.close()
    body = google_server.calendar["created"][0]
    assert body["extendedProperties"]["private"]["created_by"] == FRIDAY_STAMP
    assert body["summary"] == "Reminder" and "dateTime" in body["start"]


async def test_update_and_delete_refuse_an_event_friday_did_not_create(google_server):
    google_server.calendar["byid"] = {"human": gcal_event(id="human", stamped=False)}
    session, cal = await _calendar(google_server)
    try:
        with pytest.raises(PermissionDenied):
            await cal.update("human", summary="hijacked")
        with pytest.raises(PermissionDenied):
            await cal.delete("human")
    finally:
        await session.close()
    # The refusal happens before any write is attempted.
    assert not any(c["method"] in ("PATCH", "DELETE") for c in google_server.calls)


async def test_update_and_delete_allow_a_stamped_event(google_server):
    google_server.calendar["byid"] = {"mine": gcal_event(id="mine", stamped=True)}
    session, cal = await _calendar(google_server)
    try:
        await cal.update("mine", summary="moved")
        await cal.delete("mine")
    finally:
        await session.close()
    assert {c["method"] for c in google_server.calls} >= {"PATCH", "DELETE"}


async def test_in_meeting_is_true_only_inside_a_timed_event(google_server):
    now = time.time()
    google_server.calendar["events"] = [
        {"id": "now", "summary": "Live", "start": {"dateTime": _iso(now - 300)},
         "end": {"dateTime": _iso(now + 300)}, "organizer": {}, "htmlLink": ""}]
    session, cal = await _calendar(google_server)
    try:
        assert await cal.in_meeting(now) is True
        assert await cal.in_meeting(now + 3600) is False
    finally:
        await session.close()


async def test_an_all_day_event_is_not_a_meeting(google_server):
    """A week-long 'Conference' entry must not silence the phone for a week."""
    now = time.time()
    google_server.calendar["events"] = [gcal_event(id="ooo", summary="Conference", all_day=True,
                                                   start=_iso(now - 86400), end=_iso(now + 86400))]
    session, cal = await _calendar(google_server)
    try:
        assert await cal.in_meeting(now) is False
    finally:
        await session.close()


def _iso(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/sources/test_google.py -q`
Expected: `ModuleNotFoundError: friday.sentinel.sources.google`.

- [ ] **Step 3: Implement**

`friday/sentinel/sources/google.py`:
```python
"""Gmail and Google Calendar over plain REST.

Gmail is read-and-draft: the scopes granted are readonly and compose, and no
code path here constructs a send request. That guarantee is a scope promise
rather than a construction — `tests/sentinel/sources/test_guardrails.py` parses
this module and fails if any executable string names a send endpoint.

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
        events = []
        for raw in body.get("items") or []:
            events.append(self._to_event(raw))
        return events

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
```

Extend `friday/sentinel/sources/__init__.py` with
`from friday.sentinel.sources.google import FRIDAY_STAMP, CalendarGuarded, GmailAccount` and add
the names to `__all__`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/sources -q`
Expected: all PASS.

---

### Task 5: `ImapAccount` and the send guardrails (`sources/imap.py`)

**Files:**
- Create: `friday/sentinel/sources/imap.py`, `tests/sentinel/sources/test_guardrails.py`
- Modify: `friday/sentinel/sources/__init__.py`
- Test: `tests/sentinel/sources/test_imap.py`

**Interfaces:**
- Consumes: shapes and text helpers (Task 1).
- Produces: `ImapAccount(host, port, user, password, *, drafts_folder="Drafts", connect=imaplib.IMAP4_SSL, timeout_s=20.0)` with `name = "imap"`, `async list_unread(limit=25, since_uid=0) -> list[MailMessage]`, `async fetch(uid) -> MailMessage | None`, `async create_draft(to, subject, body, thread_id=None) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/sources/test_imap.py`:
```python
import email
import pytest

from friday.sentinel.sources import SourceError
from friday.sentinel.sources.imap import ImapAccount


def raw_message(subject="Status update", sender="Ada <ada@example.com>",
                plain="the plain body", html=None,
                date="Thu, 26 Sep 2026 09:30:00 +0200"):
    from email.message import EmailMessage
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = date
    if plain is not None:
        message.set_content(plain)
    if html is not None:
        if plain is None:
            message.set_content("placeholder")
            message.clear_content()
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    return message.as_bytes()


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: records commands, returns canned data."""

    instances = []

    def __init__(self, host, port=993, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.commands: list[tuple] = []
        self.messages: dict[str, bytes] = {}
        self.uids: list[bytes] = []
        self.appended: list[tuple] = []
        self.fail_on: str | None = None
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.commands.append(("login", user))
        if self.fail_on == "login":
            raise OSError("auth failed")
        return "OK", [b"welcome"]

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, *args))
        if self.fail_on == command.lower():
            raise OSError(f"{command} failed")
        if command.upper() == "SEARCH":
            return "OK", [b" ".join(self.uids)]
        if command.upper() == "FETCH":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            body = self.messages.get(uid)
            if body is None:
                return "NO", [None]
            return "OK", [(b"1 (UID %s BODY[])" % uid.encode(), body), b")"]
        return "OK", [None]

    def append(self, mailbox, flags, date_time, message):
        self.commands.append(("append", mailbox, flags))
        self.appended.append((mailbox, flags, message))
        return "OK", [b"[APPENDUID 1 42] done"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture(autouse=True)
def reset_fake():
    FakeIMAP.instances = []
    yield
    FakeIMAP.instances = []


def account(**kw):
    return ImapAccount("imap.example.com", 993, "vince", "app-password",
                       connect=FakeIMAP, **kw)


async def test_list_unread_maps_messages_and_never_marks_them_read():
    acct = account()
    FakeIMAP.instances = []
    imap = None

    async def run():
        nonlocal imap
        messages = await acct.list_unread(limit=10)
        imap = FakeIMAP.instances[0]
        return messages

    FakeIMAP.messages_seed = None
    # Seed through a pre-created instance: ImapAccount builds one per call.
    original_init = FakeIMAP.__init__

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.uids = [b"11", b"12"]
        self.messages = {"11": raw_message(), "12": raw_message(subject="Second")}
    FakeIMAP.__init__ = seeded_init
    try:
        messages = await run()
    finally:
        FakeIMAP.__init__ = original_init

    assert [m.subject for m in messages] == ["Second", "Status update"]     # newest first
    assert messages[0].account == "imap" and messages[0].uid == "12"
    assert messages[1].snippet == "the plain body"
    assert messages[0].ts > 0 and messages[0].url == ""
    peeked = [c for c in imap.commands if c[0] == "uid" and c[1].upper() == "FETCH"]
    assert peeked and all("BODY.PEEK[]" in str(c) for c in peeked)          # never sets \Seen
    assert ("select", "INBOX", True) in imap.commands                        # readonly
    assert imap.logged_out is True


async def test_html_only_mail_still_yields_a_snippet():
    original_init = FakeIMAP.__init__

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.uids = [b"20"]
        self.messages = {"20": raw_message(plain=None, html="<p>Hello <b>there</b></p>")}
    FakeIMAP.__init__ = seeded_init
    try:
        messages = await account().list_unread()
    finally:
        FakeIMAP.__init__ = original_init
    assert messages[0].snippet == "Hello there"


async def test_encoded_headers_are_decoded():
    original_init = FakeIMAP.__init__

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.uids = [b"21"]
        self.messages = {"21": raw_message(subject="=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=")}
    FakeIMAP.__init__ = seeded_init
    try:
        messages = await account().list_unread()
    finally:
        FakeIMAP.__init__ = original_init
    assert messages[0].subject == "Schöne Grüße"


async def test_a_huge_body_is_truncated():
    original_init = FakeIMAP.__init__

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.uids = [b"22"]
        self.messages = {"22": raw_message(plain="x" * 1_000_000)}
    FakeIMAP.__init__ = seeded_init
    try:
        messages = await account().list_unread()
    finally:
        FakeIMAP.__init__ = original_init
    assert len(messages[0].snippet) <= 500


async def test_since_uid_narrows_the_search():
    original_init = FakeIMAP.__init__
    captured = {}

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.uids = []
        captured["imap"] = self
    FakeIMAP.__init__ = seeded_init
    try:
        await account().list_unread(since_uid=100)
    finally:
        FakeIMAP.__init__ = original_init
    search = [c for c in captured["imap"].commands if c[1].upper() == "SEARCH"][0]
    assert "101:*" in " ".join(str(part) for part in search)


async def test_create_draft_appends_to_the_drafts_folder():
    original_init = FakeIMAP.__init__
    captured = {}

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        captured["imap"] = self
    FakeIMAP.__init__ = seeded_init
    try:
        uid = await account(drafts_folder="[Gmail]/Drafts").create_draft(
            "ada@example.com", "Re: status", "On it.")
    finally:
        FakeIMAP.__init__ = original_init
    mailbox, flags, raw = captured["imap"].appended[0]
    assert mailbox == "[Gmail]/Drafts" and "\\Draft" in flags
    parsed = email.message_from_bytes(raw)
    assert parsed["To"] == "ada@example.com" and parsed["Subject"] == "Re: status"
    assert "On it." in parsed.get_content()
    assert uid == "42"


async def test_a_failure_is_a_source_error_without_the_password():
    original_init = FakeIMAP.__init__

    def seeded_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.fail_on = "login"
    FakeIMAP.__init__ = seeded_init
    try:
        with pytest.raises(SourceError) as excinfo:
            await account().list_unread()
    finally:
        FakeIMAP.__init__ = original_init
    assert "app-password" not in str(excinfo.value)
```

`tests/sentinel/sources/test_guardrails.py`:
```python
"""The two promises that keep this sub-project safe, enforced mechanically."""
import ast
import pathlib

import friday
import friday.sentinel.sources as sources_pkg

FRIDAY_ROOT = pathlib.Path(friday.__file__).resolve().parent
SOURCES_ROOT = pathlib.Path(sources_pkg.__file__).resolve().parent


def _python_files(root: pathlib.Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _executable_string_literals(path: pathlib.Path) -> list[str]:
    """Every string constant that is not a docstring — prose may discuss what
    the code may not do."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_nothing_in_friday_can_send_mail():
    """Structural for IMAP: with no SMTP client and no SMTP credential in the
    registry, sending is not something this program can do."""
    offenders = []
    for path in _python_files(FRIDAY_ROOT):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names
                              if a.name.split(".")[0] == "smtplib"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "smtplib":
                offenders.append(f"{path.name}: from smtplib")
    assert not offenders, offenders


def test_no_source_module_names_a_send_endpoint():
    """The Gmail guarantee is a scope promise, so this is the thing enforcing it."""
    offenders = []
    for path in _python_files(SOURCES_ROOT):
        for literal in _executable_string_literals(path):
            if "send" in literal.lower():
                offenders.append(f"{path.name}: {literal[:60]!r}")
    assert not offenders, offenders


def test_the_guardrail_would_actually_catch_a_violation(tmp_path):
    """A test that cannot fail is not a guardrail."""
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text('"""A docstring may mention messages/send."""\nPATH = "/gmail/v1/users/me/messages/send"\n')
    literals = _executable_string_literals(sneaky)
    assert any("send" in s for s in literals)
    assert not any("docstring" in s for s in literals)     # docstrings are excluded
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/sources/test_imap.py tests/sentinel/sources/test_guardrails.py -q`
Expected: `ModuleNotFoundError: friday.sentinel.sources.imap` (the guardrail tests pass already — they are a standing constraint, not a new feature).

- [ ] **Step 3: Implement**

`friday/sentinel/sources/imap.py`:
```python
"""A mailbox over IMAP: read unread, write drafts, and nothing else.

This is the strong guarantee. There is no SMTP host, port or credential in the
settings registry, so there is nothing to send with — drafts are written by
APPENDing to the Drafts folder, exactly as a mail client does when you close a
composer without sending.

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

    # ------------------------------------------------------------- mapping

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
                           snippet=_body_snippet(parsed), ts=ts, url="", thread_id=parsed.get("Message-ID"))
```

Extend `friday/sentinel/sources/__init__.py` with
`from friday.sentinel.sources.imap import ImapAccount` and add it to `__all__`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/sources -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 6: Storage v5 and the `sources` registry group

**Files:**
- Modify: `friday/core/storage.py`, `friday/sentinel/settings_registry.py`, `friday/sentinel/monitors.py`, `.env.template`
- Test: `tests/core/test_storage_v5.py`; update `tests/core/test_storage.py`, `tests/core/test_storage_v2.py`, `tests/core/test_storage_v3.py`, `tests/core/test_storage_v4.py`, `tests/sentinel/test_settings_registry.py`

**Interfaces:**
- Produces `@dataclass(frozen=True) WatchRow(id, source, external_id, title, snippet, who, url, ts, first_seen, meta)`.
- Produces on `Store` and `AsyncStore`: `watch_item_add(item: WatchItem, first_seen: float) -> bool` (False when the id exists — the insert *is* the dedupe), `watch_items_list(source=None, limit=100) -> list[WatchRow]`, `watch_item_get(id) -> WatchRow | None`, `watch_counts(since_ts) -> dict[str, int]`, `watch_items_prune(before_ts) -> int`.
- Produces registry keys in group `sources` (see the table below); `GROUP_ORDER == ("llm", "desktop", "sentinel", "sources", "voice", "controls")`.
- `Housekeeping` also prunes `watch_items` older than `sentinel.retention_days`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_storage_v5.py`:
```python
import pytest

from friday.core import storage
from friday.core.storage import AsyncStore, Store
from friday.sentinel.sources import WatchItem


def item(source="email", external_id="gmail:abc", **kw):
    base = dict(title="Subject", snippet="body", who="Ada", url="https://x/1", ts=100.0, meta={"a": 1})
    return WatchItem(source=source, external_id=external_id, **{**base, **kw})


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v4_database_upgrades_to_v5(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {k: v for k, v in storage.MIGRATIONS.items() if k <= 4})
    old = Store.open(path)
    old.conversation_create("c1", "t", ts=1.0)
    old.kv_set("keep", {"me": True})
    assert old.schema_version() == 4
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 5
        assert s.conversation_get("c1") is not None and s.kv_get("keep") == {"me": True}
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "watch_items" in names
    finally:
        s.close()


def test_add_returns_false_for_a_repeat_and_that_is_the_dedupe(store):
    assert store.watch_item_add(item(), first_seen=1.0) is True
    assert store.watch_item_add(item(), first_seen=2.0) is False
    rows = store.watch_items_list()
    assert len(rows) == 1 and rows[0].first_seen == 1.0        # the first sighting wins


def test_two_mail_accounts_with_the_same_raw_id_do_not_collide(store):
    """A Gmail id and an IMAP UID can both be '12345'. Both must land."""
    assert store.watch_item_add(item(external_id="gmail:12345"), first_seen=1.0) is True
    assert store.watch_item_add(item(external_id="imap:12345"), first_seen=1.0) is True
    assert {r.id for r in store.watch_items_list()} == {"email:gmail:12345", "email:imap:12345"}


def test_rows_round_trip_including_meta(store):
    store.watch_item_add(item(meta={"thread_id": "t1", "unread": 3}), first_seen=5.0)
    row = store.watch_item_get("email:gmail:abc")
    assert row.source == "email" and row.external_id == "gmail:abc" and row.who == "Ada"
    assert row.ts == 100.0 and row.first_seen == 5.0 and row.meta == {"thread_id": "t1", "unread": 3}


def test_list_filters_by_source_and_orders_newest_first(store):
    store.watch_item_add(item(external_id="gmail:1", ts=10.0), first_seen=10.0)
    store.watch_item_add(item(source="jira", external_id="OPS-1", ts=30.0), first_seen=30.0)
    store.watch_item_add(item(external_id="gmail:2", ts=20.0), first_seen=20.0)
    assert [r.id for r in store.watch_items_list()] == ["jira:OPS-1", "email:gmail:2", "email:gmail:1"]
    assert [r.id for r in store.watch_items_list(source="email")] == ["email:gmail:2", "email:gmail:1"]
    assert [r.id for r in store.watch_items_list(limit=1)] == ["jira:OPS-1"]


def test_counts_and_prune(store):
    store.watch_item_add(item(external_id="gmail:old"), first_seen=10.0)
    store.watch_item_add(item(external_id="gmail:new"), first_seen=100.0)
    store.watch_item_add(item(source="jira", external_id="OPS-2"), first_seen=100.0)
    assert store.watch_counts(since_ts=50.0) == {"email": 1, "jira": 1}
    assert store.watch_counts(since_ts=0.0) == {"email": 2, "jira": 1}
    assert store.watch_items_prune(before_ts=50.0) == 1
    assert {r.id for r in store.watch_items_list()} == {"email:gmail:new", "jira:OPS-2"}


def test_a_giant_snippet_is_stored_as_given_but_the_capability_already_capped_it(store):
    """Storage does not truncate; the capability did. This pins the contract so a
    future caller that skips clean_text is visible in review, not in the database."""
    store.watch_item_add(item(snippet="x" * 500), first_seen=1.0)
    assert len(store.watch_item_get("email:gmail:abc").snippet) == 500


async def test_async_twins(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        assert await s.watch_item_add(item(), first_seen=1.0) is True
        assert await s.watch_item_add(item(), first_seen=1.0) is False
        assert (await s.watch_item_get("email:gmail:abc")).who == "Ada"
        assert len(await s.watch_items_list()) == 1
        assert await s.watch_counts(since_ts=0.0) == {"email": 1}
        assert await s.watch_items_prune(before_ts=10.0) == 1
    finally:
        await s.aclose()
```

Append to `tests/sentinel/test_settings_registry.py`:
```python
def test_sources_keys():
    from friday.sentinel.settings_registry import GROUP_ORDER
    assert GROUP_ORDER == ("llm", "desktop", "sentinel", "sources", "voice", "controls")
    for key in ("sources.google.client_secret", "sources.google.refresh_token",
                "sources.imap.password", "sources.jira.api_token"):
        assert spec_for(key).secret is True, key
    for key in ("sources.google.client_id", "sources.imap.host", "sources.imap.user",
                "sources.jira.base_url", "sources.jira.email", "sources.jira.jql"):
        assert spec_for(key).secret is False, key
    assert spec_for("sources.imap.port").default == 993
    assert spec_for("sources.imap.drafts_folder").default == "Drafts"
    assert spec_for("sources.email_interval_s").default == 120.0
    assert spec_for("sources.calendar_interval_s").default == 300.0
    assert spec_for("sources.jira_interval_s").default == 300.0
    assert spec_for("sources.calendar_horizon_min").default == 120
    assert spec_for("sources.max_backoff_s").default == 900.0
    from friday.sentinel.sources.jira import DEFAULT_JQL
    assert spec_for("sources.jira.jql").default == DEFAULT_JQL
    assert [g["name"] for g in schema()] == ["llm", "desktop", "sentinel", "sources", "voice", "controls"]


@pytest.mark.parametrize("key,raw", [
    ("sources.email_interval_s", "10"),          # below the 30 s floor
    ("sources.email_interval_s", "99999"),
    ("sources.calendar_horizon_min", "1"),
    ("sources.max_backoff_s", "5"),
    ("sources.imap.port", "70000"),
])
def test_sources_ranges_are_enforced(key, raw):
    with pytest.raises(SettingValidationError):
        validate(spec_for(key), raw)


def test_there_is_no_smtp_setting_anywhere():
    """Structural guarantee: nothing can be configured to send mail."""
    from friday.sentinel.settings_registry import REGISTRY
    assert not [s.key for s in REGISTRY if "smtp" in s.key.lower()]
```

Also update the three existing group-order assertions in that file (`test_schema_has_groups_and_no_values`, `test_sentinel_and_monitor_keys`, `test_voice_keys`) to the new six-group order.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_storage_v5.py tests/sentinel/test_settings_registry.py -q`
Expected: `schema_version() == 4`, missing `watch_item_add`, missing `sources.*` keys.

- [ ] **Step 3: Implement the migration and rows**

In `friday/core/storage.py` add migration 5 after the `4:` entry:
```python
    5: (
        "CREATE TABLE watch_items ("
        "  id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,"
        "  title TEXT NOT NULL, snippet TEXT NOT NULL, who TEXT NOT NULL, url TEXT NOT NULL,"
        "  ts REAL NOT NULL, first_seen REAL NOT NULL, meta TEXT NOT NULL)",
        "CREATE INDEX watch_items_seen ON watch_items(first_seen DESC)",
        "CREATE INDEX watch_items_source_ts ON watch_items(source, ts DESC)",
    ),
```
Add the row type after `MessageRow`:
```python
@dataclass(frozen=True)
class WatchRow:
    id: str
    source: str
    external_id: str
    title: str
    snippet: str
    who: str
    url: str
    ts: float
    first_seen: float
    meta: dict
```
and next to `_row_to_message`:
```python
_WATCH_COLUMNS = "id, source, external_id, title, snippet, who, url, ts, first_seen, meta"


def _row_to_watch(row: sqlite3.Row) -> WatchRow:
    return WatchRow(row["id"], row["source"], row["external_id"], row["title"], row["snippet"],
                    row["who"], row["url"], row["ts"], row["first_seen"], json.loads(row["meta"]))
```
Add to `Store`, after the conversation section:
```python
    # ----------------------------------------------------------- watch items

    def watch_item_add(self, item: Any, first_seen: float) -> bool:
        """True when the item is new. The INSERT is the dedupe: a source polled
        every two minutes re-offers the same message, and only the first lands."""
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO watch_items ({_WATCH_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, item.source, item.external_id, item.title, item.snippet, item.who,
             item.url, item.ts, first_seen, json.dumps(dict(item.meta))))
        return cursor.rowcount > 0

    def watch_item_get(self, id: str) -> WatchRow | None:
        row = self._conn.execute(
            f"SELECT {_WATCH_COLUMNS} FROM watch_items WHERE id = ?", (id,)).fetchone()
        return None if row is None else _row_to_watch(row)

    def watch_items_list(self, source: str | None = None, limit: int = 100) -> list[WatchRow]:
        where = "WHERE source = ?" if source else ""
        params: tuple = (source, limit) if source else (limit,)
        rows = self._conn.execute(
            f"SELECT {_WATCH_COLUMNS} FROM watch_items {where} "
            "ORDER BY first_seen DESC, id LIMIT ?", params).fetchall()
        return [_row_to_watch(r) for r in rows]

    def watch_counts(self, since_ts: float) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) FROM watch_items WHERE first_seen >= ? GROUP BY source",
            (since_ts,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def watch_items_prune(self, before_ts: float) -> int:
        return self._conn.execute(
            "DELETE FROM watch_items WHERE first_seen < ?", (before_ts,)).rowcount
```
and the async twins at the end of `AsyncStore`:
```python
    async def watch_item_add(self, item: Any, first_seen: float) -> bool:
        return await self.run(self._store.watch_item_add, item, first_seen)

    async def watch_item_get(self, id: str) -> WatchRow | None:
        return await self.run(self._store.watch_item_get, id)

    async def watch_items_list(self, source: str | None = None, limit: int = 100) -> list[WatchRow]:
        return await self.run(self._store.watch_items_list, source, limit)

    async def watch_counts(self, since_ts: float) -> dict[str, int]:
        return await self.run(self._store.watch_counts, since_ts)

    async def watch_items_prune(self, before_ts: float) -> int:
        return await self.run(self._store.watch_items_prune, before_ts)
```

Bump the existing schema assertions from `== 4` to `== 5`: three in `tests/core/test_storage.py`, one each in `test_storage_v2.py`, `test_storage_v3.py`, `test_storage_v4.py`.

In `friday/sentinel/monitors.py`, `Housekeeping.run` also prunes watch items — replace the
counts line and the log line:
```python
            counts = await ctx.store.prune(now - retention_days * 86400)
            sessions = await ctx.store.sessions_prune(now)
            chats = await ctx.store.conversations_prune(now - chat_days * 86400)
            watched = await ctx.store.watch_items_prune(now - retention_days * 86400)
            await ctx.store.checkpoint("PASSIVE")
            ctx.logger.info("housekeeping: pruned %d telemetry rows, %d events, %d sessions, "
                            "%d chats, %d watch items",
                            counts["telemetry"], counts["events"], sessions, chats, watched)
```

- [ ] **Step 4: Implement the registry group**

In `friday/sentinel/settings_registry.py` set
`GROUP_ORDER = ("llm", "desktop", "sentinel", "sources", "voice", "controls")` and add, before
the `voice.*` block:
```python
    SettingSpec("sources.google.client_id", "str", "sources",
                "OAuth client id from your Google Cloud project"),
    SettingSpec("sources.google.client_secret", "str", "sources",
                "OAuth client secret", secret=True),
    SettingSpec("sources.google.refresh_token", "str", "sources",
                "Written by 'friday-sentinel google-auth'; grants Gmail read+compose and Calendar events",
                secret=True),
    SettingSpec("sources.imap.host", "str", "sources", "IMAP server for the personal mailbox"),
    SettingSpec("sources.imap.port", "int", "sources", "IMAP port", default=993,
                validator=_range(1, 65535)),
    SettingSpec("sources.imap.user", "str", "sources", "IMAP username"),
    SettingSpec("sources.imap.password", "str", "sources",
                "IMAP app password. There is deliberately no SMTP setting: FRIDAY cannot send.",
                secret=True),
    SettingSpec("sources.imap.drafts_folder", "str", "sources",
                "Where drafts are appended (Gmail over IMAP uses '[Gmail]/Drafts')",
                default="Drafts"),
    SettingSpec("sources.jira.base_url", "url", "sources",
                "Jira Cloud base URL, e.g. https://yourteam.atlassian.net"),
    SettingSpec("sources.jira.email", "str", "sources", "Atlassian account email"),
    SettingSpec("sources.jira.api_token", "str", "sources", "Atlassian API token", secret=True),
    SettingSpec("sources.jira.jql", "str", "sources",
                "What counts as worth watching. Edit freely; an invalid query shows on the "
                "Watching card rather than retrying.",
                default=DEFAULT_JQL),
    SettingSpec("sources.email_interval_s", "float", "sources",
                "Seconds between mailbox polls", default=120.0, validator=_range(30, 3600)),
    SettingSpec("sources.calendar_interval_s", "float", "sources",
                "Seconds between calendar polls", default=300.0, validator=_range(30, 3600)),
    SettingSpec("sources.jira_interval_s", "float", "sources",
                "Seconds between Jira polls", default=300.0, validator=_range(30, 3600)),
    SettingSpec("sources.calendar_horizon_min", "int", "sources",
                "How far ahead calendar events are surfaced, in minutes",
                default=120, validator=_range(5, 1440)),
    SettingSpec("sources.max_backoff_s", "float", "sources",
                "Longest gap between retries for a degraded source",
                default=900.0, validator=_range(60, 7200)),
```
with `from friday.sentinel.sources.jira import DEFAULT_JQL` at the top of the module.

In `.env.template`, no new variables: every one of these is vault-only. Add one comment line
under the legacy section so the file does not imply otherwise:
```
# Mail, calendar and Jira credentials live in the vault, not here — set them in
# the dashboard under Settings → sources (or with friday-sentinel google-auth).
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/core tests/sentinel -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 7: Monitors and the watch runner (`watch.py`)

**Files:**
- Create: `friday/sentinel/watch.py`
- Modify: `friday/sentinel/daemon.py`
- Test: `tests/sentinel/test_watch.py`

**Interfaces:**
- Consumes: every capability (Tasks 2–5), `watch_item_add` / `kv_get` / `kv_set` (Task 6), `HandlerContext`, `RuntimeConfig`.
- Produces: `WatchContext(services, store, bus, logger)`; `class Monitor(Protocol)` with `name: str`, `interval_key: str`, `async poll(ctx) -> list[WatchItem]`; `Sources(services)` with `gmail()`, `imap()`, `calendar()`, `jira()` each returning the capability or `None` when unconfigured, and `close()`; `EmailMonitor`, `CalendarMonitor`, `JiraMonitor`; `WatchRunner(services, monitors=None, *, interval_override_s=None)` with `name = "watch"`, `async run(ctx)`, and `status() -> dict[str, dict]`; `SourceState` values `"watching" | "disabled" | "unconfigured" | "degraded" | "needs_reauth"`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_watch.py`:
```python
import asyncio
import logging
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from friday.sentinel.handlers import HandlerContext
from friday.sentinel.sources import AuthExpired, SourceError, WatchItem
from friday.sentinel.sources.jira import InvalidQuery
from friday.sentinel.watch import CalendarMonitor, EmailMonitor, JiraMonitor, WatchContext, WatchRunner


def item(source="email", external_id="gmail:1", ts=100.0, **kw):
    base = dict(title="Subject", snippet="body", who="Ada", url="", meta={})
    return WatchItem(source=source, external_id=external_id, ts=ts, **{**base, **kw})


class FakeMonitor:
    """A Monitor whose behaviour each test scripts."""

    def __init__(self, name="email", results=None):
        self.name = name
        self.interval_key = "sources.email_interval_s"
        self.results = list(results or [])
        self.polls = 0

    async def poll(self, ctx):
        self.polls += 1
        if not self.results:
            return []
        step = self.results.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
async def rig(services):
    ctx = HandlerContext(settings=services.settings, store=services.store, bus=services.bus,
                         logger=logging.getLogger("test.watch"))
    # Poll fast and switch everything on.
    await services.config.set_many({"sources.email_interval_s": 30, "controls.monitors.email": True,
                                    "controls.monitors.calendar": True,
                                    "controls.monitors.jira": True}, actor="test")
    return SimpleNamespace(services=services, ctx=ctx)


async def _run_once(runner, ctx, *, ticks=1, timeout=3.0):
    """Let the runner complete `ticks` polls, then stop it."""
    task = asyncio.create_task(runner.run(ctx))
    deadline = time.monotonic() + timeout
    try:
        while runner.polls_completed < ticks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ------------------------------------------------------------------ runner

async def test_new_items_are_stored_once_and_published_once(rig):
    monitor = FakeMonitor(results=[[item(), item(external_id="gmail:2")], [item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=2)

    rows = await rig.services.store.watch_items_list()
    assert {r.id for r in rows} == {"email:gmail:1", "email:gmail:2"}
    events = await rig.services.store.list_events(type="monitor.item", limit=50)
    assert len(events) == 2                       # the repeat on tick two published nothing
    payload = events[0].event.payload
    assert payload["source"] == "email" and payload["title"] == "Subject"


async def test_two_accounts_with_the_same_raw_id_both_land(rig):
    monitor = FakeMonitor(results=[[item(external_id="gmail:12345"),
                                    item(external_id="imap:12345")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    assert len(await rig.services.store.watch_items_list()) == 2
    assert len(await rig.services.store.list_events(type="monitor.item", limit=50)) == 2


async def test_a_disabled_monitor_is_not_polled(rig):
    await rig.services.config.set_many({"controls.monitors.email": False}, actor="test")
    monitor = FakeMonitor(results=[[item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    task = asyncio.create_task(runner.run(rig.ctx))
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert monitor.polls == 0
    assert runner.status()["email"]["state"] == "disabled"


async def test_the_switch_takes_effect_without_a_restart(rig):
    await rig.services.config.set_many({"controls.monitors.email": False}, actor="test")
    monitor = FakeMonitor(results=[[item()], [item(external_id="gmail:2")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    task = asyncio.create_task(runner.run(rig.ctx))
    try:
        await asyncio.sleep(0.08)
        assert monitor.polls == 0
        await rig.services.config.set_many({"controls.monitors.email": True}, actor="test")
        deadline = time.monotonic() + 3
        while monitor.polls == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert monitor.polls >= 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_transient_failure_degrades_only_that_source(rig):
    bad = FakeMonitor(name="jira", results=[SourceError("Atlassian said 502"), [item(source="jira",
                                                                                    external_id="OPS-1")]])
    bad.interval_key = "sources.jira_interval_s"
    good = FakeMonitor(results=[[item()], [item(external_id="gmail:2")]])
    runner = WatchRunner(rig.services, monitors=[bad, good], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=3)

    assert runner.status()["email"]["state"] == "watching"
    errors = await rig.services.store.list_events(type="monitor.error", limit=10)
    assert errors and errors[0].event.payload["source"] == "jira"
    assert errors[0].event.payload["kind"] == "transient"
    assert {r.source for r in await rig.services.store.watch_items_list()} >= {"email"}


async def test_auth_failure_parks_the_source_and_says_what_to_do(rig):
    monitor = FakeMonitor(results=[AuthExpired("google refused the refresh token")])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    state = runner.status()["email"]
    assert state["state"] == "needs_reauth" and "google-auth" in state["last_error"]
    errors = await rig.services.store.list_events(type="monitor.error", limit=10)
    assert errors[0].event.payload["kind"] == "auth"


async def test_a_bad_jql_parks_like_an_auth_failure(rig):
    monitor = FakeMonitor(name="jira", results=[InvalidQuery("invalid JQL: unexpected token")])
    monitor.interval_key = "sources.jira_interval_s"
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    state = runner.status()["jira"]
    assert state["state"] == "degraded" and "invalid JQL" in state["last_error"]


async def test_one_error_event_per_transition_not_per_tick(rig):
    monitor = FakeMonitor(results=[SourceError("down")] * 4)
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=3)
    errors = await rig.services.store.list_events(type="monitor.error", limit=20)
    assert len(errors) == 1                       # an hour of downtime is one event, not sixty


async def test_recovery_clears_the_state(rig):
    monitor = FakeMonitor(results=[SourceError("down"), [item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=2)
    assert runner.status()["email"]["state"] == "watching"
    assert runner.status()["email"]["last_error"] == ""


async def test_a_malformed_item_is_skipped_not_fatal(rig):
    monitor = FakeMonitor(results=[[item(), "not an item", item(external_id="gmail:3")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    assert {r.id for r in await rig.services.store.watch_items_list()} == {"email:gmail:1", "email:gmail:3"}


async def test_status_reports_every_monitor_for_the_dashboard(rig):
    runner = WatchRunner(rig.services, monitors=[FakeMonitor(), FakeMonitor(name="jira")],
                         interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    status = runner.status()
    assert set(status) == {"email", "jira"}
    assert set(status["email"]) == {"state", "enabled", "configured", "last_poll", "last_error",
                                    "items_today"}


# --------------------------------------------------------------- monitors

class FakeGmail:
    name = "gmail"

    def __init__(self, messages):
        self._messages = messages

    async def list_unread(self, limit=25):
        return list(self._messages)


class FakeImap(FakeGmail):
    name = "imap"

    async def list_unread(self, limit=25, since_uid=0):
        self.since_uid = since_uid
        return [m for m in self._messages if int(m.uid) > since_uid]


async def test_email_monitor_qualifies_ids_by_account(rig):
    from friday.sentinel.sources import MailMessage

    gmail = FakeGmail([MailMessage("gmail", "12345", "Work", "boss@x", "hi", 10.0, "u", "t")])
    imap = FakeImap([MailMessage("imap", "12345", "Personal", "mum@x", "hello", 20.0, "", None)])
    monitor = EmailMonitor(lambda: gmail, lambda: imap)
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    assert {i.external_id for i in items} == {"gmail:12345", "imap:12345"}
    assert {i.id for i in items} == {"email:gmail:12345", "email:imap:12345"}
    assert all(i.source == "email" for i in items)
    assert {i.meta["account"] for i in items} == {"gmail", "imap"}


async def test_email_monitor_advances_the_imap_cursor(rig):
    from friday.sentinel.sources import MailMessage

    imap = FakeImap([MailMessage("imap", "7", "a", "x", "s", 1.0, "", None),
                     MailMessage("imap", "9", "b", "x", "s", 2.0, "", None)])
    monitor = EmailMonitor(lambda: None, lambda: imap)
    ctx = WatchContext(rig.services, rig.services.store, rig.services.bus, logging.getLogger("t"))
    await monitor.poll(ctx)
    assert await rig.services.store.kv_get("watch.cursor.imap") == 9
    await monitor.poll(ctx)
    assert imap.since_uid == 9                    # the next search starts after the last UID


async def test_calendar_monitor_uses_the_configured_horizon(rig):
    from friday.sentinel.sources import CalendarEvent

    captured = {}

    class FakeCalendar:
        async def list(self, time_min, time_max):
            captured["window"] = (time_min, time_max)
            return [CalendarEvent("e1", "Standup", "Ada", time.time() + 600, time.time() + 1200,
                                  False, False, "https://cal/e1")]

    await rig.services.config.set_many({"sources.calendar_horizon_min": 30}, actor="test")
    monitor = CalendarMonitor(lambda: FakeCalendar())
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    span = captured["window"][1] - captured["window"][0]
    assert 1750 <= span <= 1850                   # ~30 minutes ahead
    assert items[0].source == "calendar" and items[0].title == "Standup"
    assert items[0].meta["all_day"] is False


async def test_jira_monitor_runs_the_configured_jql_and_advances_its_cursor(rig):
    from friday.sentinel.sources import JiraIssue

    seen = {}

    class FakeJira:
        async def search(self, jql, limit=50):
            seen["jql"] = jql
            return [JiraIssue("OPS-1", "Disk full", "Ada", "Highest", "Blocked", 500.0,
                              "https://j/OPS-1", False)]

    await rig.services.config.set_many({"sources.jira.jql": "assignee = currentUser()"}, actor="test")
    monitor = JiraMonitor(lambda: FakeJira())
    ctx = WatchContext(rig.services, rig.services.store, rig.services.bus, logging.getLogger("t"))
    items = await monitor.poll(ctx)
    assert seen["jql"] == "assignee = currentUser()"
    assert items[0].source == "jira" and items[0].external_id == "OPS-1"
    assert items[0].title == "OPS-1: Disk full" and items[0].meta["priority"] == "Highest"
    assert await rig.services.store.kv_get("watch.cursor.jira") == 500.0


async def test_an_unconfigured_source_yields_nothing_and_is_marked(rig):
    monitor = EmailMonitor(lambda: None, lambda: None)
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    assert items == []
    assert monitor.configured is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_watch.py -q`
Expected: `ModuleNotFoundError: friday.sentinel.watch`.

- [ ] **Step 3: Implement**

`friday/sentinel/watch.py`:
```python
"""What FRIDAY watches, and the loop that watches it.

A monitor polls one surface and returns normalised items; the runner decides
whether to poll at all (the dashboard switch), stores what is new (the insert is
the dedupe) and publishes it. A source that fails parks itself — degraded for
something worth retrying, needs_reauth for something only a human can fix — and
never takes the daemon or the other sources with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import aiohttp

from friday.core.events import Event
from friday.sentinel.sources import (AuthExpired, CalendarEvent, JiraIssue, MailMessage,
                                     SourceError, WatchItem, clean_text)
from friday.sentinel.sources.google import CalendarGuarded, GmailAccount
from friday.sentinel.sources.imap import ImapAccount
from friday.sentinel.sources.jira import InvalidQuery, JiraReadOnly
from friday.sentinel.sources.oauth import GoogleOAuth

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 60.0


@dataclass
class WatchContext:
    services: Any
    store: Any
    bus: Any
    logger: logging.Logger


class Monitor(Protocol):
    name: str
    interval_key: str
    configured: bool

    async def poll(self, ctx: WatchContext) -> list[WatchItem]: ...


# ----------------------------------------------------------------- sources

class Sources:
    """Builds capabilities from the current vault values, sharing one HTTP
    session and one OAuth token cache. Rebuilt cheaply per poll so a credential
    edited in the dashboard takes effect on the next tick."""

    def __init__(self, services: Any) -> None:
        self._svc = services
        self._session: aiohttp.ClientSession | None = None
        self._oauth: GoogleOAuth | None = None
        self._oauth_key = ""

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _get(self, key: str) -> str:
        value = self._svc.config.get(key)
        return "" if value is None else str(value).strip()

    def _google_oauth(self) -> GoogleOAuth | None:
        client_id = self._get("sources.google.client_id")
        secret = self._get("sources.google.client_secret")
        refresh = self._get("sources.google.refresh_token")
        if not (client_id and secret and refresh):
            return None
        oauth = GoogleOAuth(self._http(), client_id, secret, refresh)
        if self._oauth is None or oauth.credentials_key != self._oauth_key:
            self._oauth, self._oauth_key = oauth, oauth.credentials_key
        return self._oauth                       # keep the cached access token across polls

    def gmail(self) -> GmailAccount | None:
        oauth = self._google_oauth()
        return None if oauth is None else GmailAccount(self._http(), oauth)

    def calendar(self) -> CalendarGuarded | None:
        oauth = self._google_oauth()
        return None if oauth is None else CalendarGuarded(self._http(), oauth)

    def imap(self) -> ImapAccount | None:
        host, user = self._get("sources.imap.host"), self._get("sources.imap.user")
        password = self._get("sources.imap.password")
        if not (host and user and password):
            return None
        return ImapAccount(host, int(self._svc.config.get("sources.imap.port") or 993),
                           user, password,
                           drafts_folder=self._get("sources.imap.drafts_folder") or "Drafts")

    def jira(self) -> JiraReadOnly | None:
        base = self._get("sources.jira.base_url")
        email_address = self._get("sources.jira.email")
        token = self._get("sources.jira.api_token")
        if not (base and email_address and token):
            return None
        return JiraReadOnly(self._http(), base, email_address, token)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------- monitors

class EmailMonitor:
    name = "email"
    interval_key = "sources.email_interval_s"

    def __init__(self, gmail: Callable[[], Any], imap: Callable[[], Any]) -> None:
        self._gmail, self._imap = gmail, imap
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        gmail, imap = self._gmail(), self._imap()
        self.configured = bool(gmail or imap)
        items: list[WatchItem] = []
        if gmail is not None:
            items += [self._to_item(m) for m in await gmail.list_unread(limit=25)]
        if imap is not None:
            cursor = int(await ctx.store.kv_get("watch.cursor.imap") or 0)
            messages = await imap.list_unread(limit=25, since_uid=cursor)
            items += [self._to_item(m) for m in messages]
            highest = max((int(m.uid) for m in messages if m.uid.isdigit()), default=cursor)
            if highest > cursor:
                await ctx.store.kv_set("watch.cursor.imap", highest)
        return items

    @staticmethod
    def _to_item(message: MailMessage) -> WatchItem:
        # The account qualifies the id: a Gmail id and an IMAP UID can both be
        # "12345", and one real message must never suppress the other.
        return WatchItem(source="email", external_id=f"{message.account}:{message.uid}",
                         title=message.subject or "(no subject)", snippet=message.snippet,
                         who=message.sender, url=message.url, ts=message.ts,
                         meta={"account": message.account, "thread_id": message.thread_id})


class CalendarMonitor:
    name = "calendar"
    interval_key = "sources.calendar_interval_s"

    def __init__(self, calendar: Callable[[], Any]) -> None:
        self._calendar = calendar
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        calendar = self._calendar()
        self.configured = calendar is not None
        if calendar is None:
            return []
        horizon = float(ctx.services.config.get("sources.calendar_horizon_min") or 120) * 60
        now = time.time()
        events = await calendar.list(now, now + horizon)
        return [self._to_item(e) for e in events]

    @staticmethod
    def _to_item(event: CalendarEvent) -> WatchItem:
        return WatchItem(source="calendar", external_id=event.id, title=event.summary,
                         snippet=clean_text(f"{event.organiser} — starts "
                                            f"{time.strftime('%H:%M', time.localtime(event.start))}"),
                         who=event.organiser, url=event.url, ts=event.start,
                         meta={"start": event.start, "end": event.end, "all_day": event.all_day,
                               "created_by_friday": event.created_by_friday})


class JiraMonitor:
    name = "jira"
    interval_key = "sources.jira_interval_s"

    def __init__(self, jira: Callable[[], Any]) -> None:
        self._jira = jira
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        jira = self._jira()
        self.configured = jira is not None
        if jira is None:
            return []
        jql = str(ctx.services.config.get("sources.jira.jql") or "")
        issues = await jira.search(jql, limit=50)
        cursor = float(await ctx.store.kv_get("watch.cursor.jira") or 0.0)
        newest = max((i.updated for i in issues), default=cursor)
        if newest > cursor:
            await ctx.store.kv_set("watch.cursor.jira", newest)
        return [self._to_item(i) for i in issues]

    @staticmethod
    def _to_item(issue: JiraIssue) -> WatchItem:
        return WatchItem(source="jira", external_id=issue.key,
                         title=f"{issue.key}: {issue.summary}",
                         snippet=clean_text(f"{issue.status} · {issue.priority} · {issue.who}"),
                         who=issue.who, url=issue.url, ts=issue.updated,
                         meta={"key": issue.key, "priority": issue.priority,
                               "status": issue.status, "is_comment": issue.is_comment})


# ------------------------------------------------------------------ runner

@dataclass
class _State:
    state: str = "watching"
    last_poll: float = 0.0
    last_error: str = ""
    failures: int = 0


class WatchRunner:
    """One supervised task that polls every monitor on its own schedule."""

    name = "watch"

    def __init__(self, services: Any, monitors: list[Monitor] | None = None, *,
                 interval_override_s: float | None = None) -> None:
        self._svc = services
        self._sources = Sources(services)
        # Registry intervals have a 30 s floor, which no test can wait for. The
        # override exists so tests can drive the loop; production passes None.
        self._override = interval_override_s
        self._monitors = monitors if monitors is not None else [
            EmailMonitor(self._sources.gmail, self._sources.imap),
            CalendarMonitor(self._sources.calendar),
            JiraMonitor(self._sources.jira),
        ]
        self._states: dict[str, _State] = {m.name: _State() for m in self._monitors}
        self.polls_completed = 0

    def status(self) -> dict[str, dict]:
        midnight = time.time() - (time.time() % 86400)
        counts = getattr(self, "_counts", {})
        return {m.name: {"state": self._states[m.name].state,
                         "enabled": bool(self._svc.config.get(f"controls.monitors.{m.name}")),
                         "configured": bool(getattr(m, "configured", False)),
                         "last_poll": self._states[m.name].last_poll,
                         "last_error": self._states[m.name].last_error,
                         "items_today": counts.get(m.name, 0)}
                for m in self._monitors}

    async def run(self, ctx: Any) -> None:
        watch_ctx = WatchContext(self._svc, self._svc.store, self._svc.bus, ctx.logger)
        try:
            await asyncio.gather(*(self._loop(m, watch_ctx) for m in self._monitors))
        finally:
            await self._sources.close()

    async def _loop(self, monitor: Monitor, ctx: WatchContext) -> None:
        while True:
            enabled = bool(self._svc.config.get(f"controls.monitors.{monitor.name}"))
            state = self._states[monitor.name]
            if not enabled:
                state.state = "disabled"
            else:
                await self._tick(monitor, ctx, state)
            await asyncio.sleep(self._override or self._interval(monitor, state))

    def _interval(self, monitor: Monitor, state: _State) -> float:
        base = float(self._svc.config.get(monitor.interval_key) or 300.0)
        if state.failures == 0:
            return base
        cap = float(self._svc.config.get("sources.max_backoff_s") or 900.0)
        if state.state == "needs_reauth":
            return cap                       # only a human can fix this; stop hammering
        return min(cap, base * (2 ** min(state.failures, 5)))

    async def _tick(self, monitor: Monitor, ctx: WatchContext, state: _State) -> None:
        previous = state.state
        try:
            async with asyncio.timeout(POLL_TIMEOUT_S):
                items = await monitor.poll(ctx)
        except asyncio.CancelledError:
            raise
        except AuthExpired as e:
            await self._fail(monitor, state, previous, "needs_reauth", "auth", str(e))
            return
        except InvalidQuery as e:
            # A query the user typed. Retrying it changes nothing; showing it does.
            await self._fail(monitor, state, previous, "degraded", "query", str(e))
            return
        except (SourceError, asyncio.TimeoutError) as e:
            message = str(e) or f"{monitor.name} poll timed out"
            await self._fail(monitor, state, previous, "degraded", "transient", message)
            return
        except Exception as e:
            await self._fail(monitor, state, previous, "degraded", "transient",
                             f"{type(e).__name__}: {e}")
            return

        state.state = "watching" if getattr(monitor, "configured", True) else "unconfigured"
        state.last_poll, state.last_error, state.failures = time.time(), "", 0
        self.polls_completed += 1
        await self._absorb(monitor, ctx, items)

    async def _absorb(self, monitor: Monitor, ctx: WatchContext, items: list[Any]) -> None:
        now = time.time()
        for item in items:
            if not isinstance(item, WatchItem):
                ctx.logger.info("watch: %s returned a non-item, skipping", monitor.name)
                continue
            try:
                if await ctx.store.watch_item_add(item, now):
                    await ctx.bus.publish(Event(type="monitor.item",
                                                source=self._svc.settings.node_id,
                                                payload=item.to_dict()))
            except Exception as e:
                ctx.logger.warning("watch: could not record %s: %s", item.id, e)
        self._counts = await ctx.store.watch_counts(now - (now % 86400))

    async def _fail(self, monitor: Monitor, state: _State, previous: str,
                    new_state: str, kind: str, message: str) -> None:
        state.state, state.last_error = new_state, message
        state.failures += 1
        state.last_poll = time.time()
        self.polls_completed += 1
        if previous == new_state:
            return                          # one event per transition, not per tick
        await self._svc.bus.publish(Event(type="monitor.error",
                                          source=self._svc.settings.node_id,
                                          payload={"source": monitor.name, "kind": kind,
                                                   "message": message}))
```

In `friday/sentinel/daemon.py`, import it and add it to the supervised monitors:
```python
from friday.sentinel.watch import WatchRunner
```
```python
            watch_runner = WatchRunner(services)
            self.watch = watch_runner
            for monitor in (TelemetryMonitor(services.config),
                            SelfHeartbeat(services.config),
                            Housekeeping(services.config),
                            watch_runner):
```
and set `self.watch: WatchRunner | None = None` in `Sentinel.__init__` next to `self.services`.
`Services` gains `watch: Any = None` (a `field(default=None)`) so the API can read
`services.watch.status()`; the daemon assigns `services.watch = watch_runner` right after
constructing it.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_watch.py tests/sentinel/test_daemon.py -q`
Expected: all PASS.

---

### Task 8: `friday-sentinel google-auth`

**Files:**
- Modify: `friday/sentinel/cli.py`
- Test: `tests/sentinel/test_cli_google_auth.py`

**Interfaces:**
- Consumes: `SCOPES` (Task 2), the `sources.google.*` registry keys (Task 6), `open_store` and the vault (existing CLI).
- Produces: subcommand `google-auth` with `--print-only` and `--revoke`; helpers `build_auth_url(client_id, redirect_uri, state) -> str`, `exchange_code(code, client_id, client_secret, redirect_uri, *, token_url=TOKEN_URL, urlopen=…) -> str` (returns the refresh token), `store_refresh_token(store, vault, token) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_cli_google_auth.py`:
```python
import json
import urllib.parse
from io import BytesIO

import pytest

from friday.core.storage import Store
from friday.sentinel import cli
from friday.sentinel.sources.oauth import SCOPES
from tests.conftest import TEST_MASTER_KEY


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    return tmp_path / "data"


def _seed_client(env, client_id="cid", secret="shh"):
    from friday.core.config import load_settings
    from friday.core.vault import Vault
    settings = load_settings()
    vault = Vault.from_master_key(settings.master_key)
    store = Store.open(env / "sentinel.db")
    try:
        store.setting_set("sources.google.client_id", json.dumps(client_id), secret=False,
                          updated_by="test", ts=1.0)
        store.setting_set("sources.google.client_secret", vault.encrypt(
            "sources.google.client_secret", secret), secret=True, updated_by="test", ts=1.0)
    finally:
        store.close()


def test_auth_url_asks_for_offline_access_and_only_our_scopes():
    url = cli.build_auth_url("cid", "http://127.0.0.1:8823", "state-1")
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["cid"] and query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"] and query["state"] == ["state-1"]
    assert query["scope"][0].split() == list(SCOPES)
    assert "gmail.send" not in query["scope"][0] and "gmail.modify" not in query["scope"][0]


def test_exchange_code_returns_the_refresh_token():
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = dict(urllib.parse.parse_qsl(request.data.decode()))
        return BytesIO(json.dumps({"refresh_token": "refresh-xyz",
                                   "access_token": "a", "expires_in": 3600}).encode())

    token = cli.exchange_code("the-code", "cid", "shh", "http://127.0.0.1:8823",
                              urlopen=fake_urlopen)
    assert token == "refresh-xyz"
    assert captured["body"]["grant_type"] == "authorization_code"
    assert captured["body"]["code"] == "the-code"


def test_exchange_code_explains_a_missing_refresh_token():
    def fake_urlopen(request, timeout=0):
        return BytesIO(json.dumps({"access_token": "a"}).encode())

    with pytest.raises(RuntimeError) as excinfo:
        cli.exchange_code("c", "cid", "shh", "http://x", urlopen=fake_urlopen)
    assert "refresh token" in str(excinfo.value).lower()


def test_google_auth_requires_the_client_credentials_first(env, capsys):
    assert cli.main(["google-auth", "--print-only"]) == 1
    assert "sources.google.client_id" in capsys.readouterr().err


def test_google_auth_print_only_does_not_touch_the_vault(env, monkeypatch, capsys):
    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-printed")
    assert cli.main(["google-auth", "--print-only"]) == 0
    out = capsys.readouterr().out
    assert "refresh-printed" in out and "sources.google.refresh_token" in out
    store = Store.open(env / "sentinel.db")
    try:
        assert store.setting_get("sources.google.refresh_token") is None
    finally:
        store.close()


def test_google_auth_stores_the_token_encrypted_and_audits_without_it(env, monkeypatch, capsys):
    from friday.core.config import load_settings
    from friday.core.vault import Vault

    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-stored")
    assert cli.main(["google-auth"]) == 0
    assert "refresh-stored" not in capsys.readouterr().out        # never echoed when stored

    vault = Vault.from_master_key(load_settings().master_key)
    store = Store.open(env / "sentinel.db")
    try:
        row = store.setting_get("sources.google.refresh_token")
        assert row.secret is True and row.value.startswith("v1:")
        assert vault.decrypt("sources.google.refresh_token", row.value) == "refresh-stored"
        entry = [a for a in store.audit_list() if a.action == "sources.google.consent"][0]
        assert "refresh-stored" not in json.dumps(entry.detail)
    finally:
        store.close()


def test_revoke_clears_the_token(env, monkeypatch, capsys):
    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-stored")
    cli.main(["google-auth"])
    assert cli.main(["google-auth", "--revoke"]) == 0
    store = Store.open(env / "sentinel.db")
    try:
        assert store.setting_get("sources.google.refresh_token") is None
    finally:
        store.close()
    assert "revoked" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_cli_google_auth.py -q`
Expected: `AttributeError: module 'friday.sentinel.cli' has no attribute 'build_auth_url'`.

- [ ] **Step 3: Implement**

In `friday/sentinel/cli.py` add the imports and the command. Put the helpers above `main`:
```python
import http.server
import json as _json
import secrets as _secrets
import urllib.parse
import urllib.request

from friday.sentinel.sources.oauth import SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_PORT = 8823
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}"


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str, *,
                  token_url: str = TOKEN_URL, urlopen=urllib.request.urlopen) -> str:
    payload = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    request = urllib.request.Request(token_url, data=payload,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=30) as response:
        body = _json.loads(response.read().decode())
    token = body.get("refresh_token")
    if not token:
        raise RuntimeError(
            "Google returned no refresh token. This happens when the account has already "
            "granted access: revoke it at https://myaccount.google.com/permissions and retry.")
    return token


def _consent_flow(client_id: str, client_secret: str) -> str:
    """Serve one redirect on the loopback, then trade the code for a refresh token."""
    state = _secrets.token_urlsafe(16)
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802 (stdlib naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            ok = captured.get("state") == state and "code" in captured
            self.wfile.write(b"FRIDAY: you can close this tab." if ok
                             else b"FRIDAY: consent failed; check the terminal.")

        def log_message(self, *args):                       # keep the terminal clean
            return

    print("Open this URL, grant access, and the browser will redirect back here:")
    print(f"  {build_auth_url(client_id, REDIRECT_URI, state)}")
    print("Waiting for the redirect…")
    with http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler) as server:
        server.timeout = 300
        server.handle_request()
    if captured.get("state") != state:
        raise RuntimeError("the redirect did not carry the expected state; nothing was stored")
    if "code" not in captured:
        raise RuntimeError(f"Google reported: {captured.get('error', 'no code returned')}")
    return exchange_code(captured["code"], client_id, client_secret, REDIRECT_URI)


def _cmd_google_auth(print_only: bool, revoke: bool) -> int:
    from friday.core.vault import Vault

    settings = load_settings()
    store = open_store(settings)
    vault = Vault.from_master_key(settings.master_key or "")
    try:
        if revoke:
            store.setting_delete("sources.google.refresh_token")
            store.audit_append(time.time(), "cli", "sources.google.consent", None,
                               {"action": "revoked"})
            print("Google refresh token revoked. Gmail and Calendar will report needs_reauth.")
            return 0

        client_id = _plain_setting(store, "sources.google.client_id")
        secret_row = store.setting_get("sources.google.client_secret")
        client_secret = vault.decrypt("sources.google.client_secret", secret_row.value) \
            if secret_row else ""
        if not client_id or not client_secret:
            print("friday-sentinel: set sources.google.client_id and sources.google.client_secret "
                  "in the dashboard (Settings → sources) before running consent.", file=sys.stderr)
            return 1

        token = _consent_flow(client_id, client_secret)
        if print_only:
            print("Paste this into Settings → sources on the sentinel:")
            print(f"  sources.google.refresh_token = {token}")
            return 0
        now = time.time()
        store.setting_set("sources.google.refresh_token",
                          vault.encrypt("sources.google.refresh_token", token),
                          secret=True, updated_by="cli", ts=now)
        store.audit_append(now, "cli", "sources.google.consent", None,
                           {"action": "stored", "scopes": list(SCOPES)})
        print("Refresh token stored in the vault. Gmail and Calendar are ready.")
        return 0
    finally:
        store.close()


def _plain_setting(store, key: str) -> str:
    row = store.setting_get(key)
    if row is None:
        return ""
    try:
        return str(_json.loads(row.value))
    except ValueError:
        return str(row.value)
```
Register the subcommand in `_parser()`:
```python
    google = sub.add_parser("google-auth", help="grant Gmail and Calendar access (one time)")
    google.add_argument("--print-only", action="store_true",
                        help="print the refresh token instead of storing it")
    google.add_argument("--revoke", action="store_true", help="forget the stored refresh token")
```
and dispatch it in `main`, next to the `token` branch:
```python
        if args.command == "google-auth":
            return _cmd_google_auth(args.print_only, args.revoke)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_cli_google_auth.py tests/sentinel/test_cli.py -q`
Expected: all PASS.

---

### Task 9: `GET /api/watch`, the Watching card and the Activity badge

**Files:**
- Modify: `friday/sentinel/web.py`, `friday/sentinel/dashboard/views/overview.js`, `friday/sentinel/dashboard/views/activity.js`, `friday/sentinel/dashboard/tailwind.src.css`, `friday/sentinel/dashboard/tailwind.css` (rebuild)
- Test: `tests/sentinel/test_watch_api.py`, `tests/sentinel/test_dashboard_files.py` (extend)

**Interfaces:**
- Consumes: `WatchRunner.status()` (Task 7), `watch_items_list` (Task 6).
- Produces: `GET /api/watch` (session only) → `{"sources": {name: {state, enabled, configured, last_poll, last_error, items_today}}, "recent": [{id, source, title, who, url, ts, first_seen}]}`; the Watching card on Overview; `monitor` → indigo and `monitor.error` → rose badges in Activity.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_watch_api.py`:
```python
import time
from types import SimpleNamespace

import pytest

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from friday.sentinel.sources import WatchItem
from tests.sentinel.conftest import node_headers

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
async def client(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    c = await aiohttp_client(create_app(services))
    assert (await c.post("/auth/login", json={"username": "vince", "password": "pw"},
                         headers=CSRF)).status == 204
    return c


def _status():
    return {"email": {"state": "watching", "enabled": True, "configured": True,
                      "last_poll": 1000.0, "last_error": "", "items_today": 4},
            "jira": {"state": "needs_reauth", "enabled": True, "configured": True,
                     "last_poll": 900.0, "last_error": "jira refused the API token",
                     "items_today": 0}}


async def test_watch_reports_sources_and_recent_items(client, services):
    services.watch = SimpleNamespace(status=_status)
    await services.store.watch_item_add(
        WatchItem(source="email", external_id="gmail:1", title="Status update", snippet="body",
                  who="Ada", url="https://mail/1", ts=50.0, meta={"account": "gmail"}), 60.0)

    body = await (await client.get("/api/watch")).json()
    assert body["sources"]["email"]["state"] == "watching"
    assert body["sources"]["jira"]["last_error"] == "jira refused the API token"
    assert len(body["recent"]) == 1
    row = body["recent"][0]
    assert row["id"] == "email:gmail:1" and row["title"] == "Status update"
    assert row["who"] == "Ada" and row["url"] == "https://mail/1" and row["first_seen"] == 60.0


async def test_watch_is_empty_before_the_runner_exists(client, services):
    services.watch = None
    body = await (await client.get("/api/watch")).json()
    assert body == {"sources": {}, "recent": []}


async def test_watch_never_leaks_a_snippet_body_to_the_list(client, services):
    """The card shows titles, not message bodies — a shoulder-surfer sees less."""
    services.watch = SimpleNamespace(status=_status)
    await services.store.watch_item_add(
        WatchItem(source="email", external_id="gmail:2", title="Subject",
                  snippet="salary details inside", who="HR", url="", ts=1.0, meta={}), 2.0)
    body = await (await client.get("/api/watch")).json()
    assert "salary" not in str(body)


async def test_watch_requires_a_session(aiohttp_client, services, node_token):
    anon = await aiohttp_client(create_app(services))
    assert (await anon.get("/api/watch")).status == 401
    assert (await anon.get("/api/watch", headers=node_headers(node_token))).status == 401
```

In `tests/sentinel/test_dashboard_files.py` append:
```python
def test_overview_renders_the_watching_card():
    src = (DASHBOARD_DIR / "views" / "overview.js").read_text()
    assert "api/watch" in src and "Watching" in src
    assert not re.search(r'["\']/api/watch', src)          # relative, like every other call


def test_activity_badges_monitor_events():
    src = (DASHBOARD_DIR / "views" / "activity.js").read_text()
    assert '"monitor"' in src
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_watch_api.py -q`
Expected: 404 on `/api/watch`.

- [ ] **Step 3: Implement the endpoint**

In `friday/sentinel/web.py`, after `telemetry_all`:
```python
async def watch_status(request: web.Request) -> web.Response:
    await require_user(request)
    svc = request.app[SERVICES]
    runner = getattr(svc, "watch", None)
    if runner is None:
        return web.json_response({"sources": {}, "recent": []})
    rows = await svc.store.watch_items_list(limit=10)
    # Titles and senders only: the card is glanceable and a body is not.
    recent = [{"id": r.id, "source": r.source, "title": r.title, "who": r.who,
               "url": r.url, "ts": r.ts, "first_seen": r.first_seen} for r in rows]
    return web.json_response({"sources": runner.status(), "recent": recent})
```
and register it beside `/api/telemetry`:
```python
        web.get("/api/watch", watch_status),
```

- [ ] **Step 4: Implement the card**

In `friday/sentinel/dashboard/views/overview.js`, add the state tone map near `REFRESH_TYPES`:
```js
const WATCH_TONE = { watching: "ok", disabled: "none", unconfigured: "none",
                     degraded: "warn", needs_reauth: "danger" };
const WATCH_LABEL = { watching: "watching", disabled: "off", unconfigured: "not configured",
                      degraded: "degraded", needs_reauth: "needs reauth" };
```
extend `REFRESH_TYPES` with the two new events:
```js
const REFRESH_TYPES = new Set(["node.heartbeat", "telemetry.sample", "sentinel.started",
                               "monitor.item", "monitor.error"]);
```
add the card builder next to `nodeCard`:
```js
  function watchCard(watch) {
    const names = Object.keys(watch.sources || {});
    const rows = names.map((name) => {
      const info = watch.sources[name];
      const tone = WATCH_TONE[info.state] || "none";
      return el("div", { class: "flex items-baseline justify-between gap-3 py-1.5" }, [
        el("span", { class: "flex items-center gap-2" }, [
          el("span", { class: cls("dot", LEVEL[tone].dot) }),
          el("span", { class: "font-mono text-xs", text: name }),
          el("span", { class: cls("text-xs", LEVEL[tone].text),
                       text: WATCH_LABEL[info.state] || info.state }),
        ]),
        el("span", { class: "min-w-0 truncate text-right text-xs text-zinc-500",
                     title: info.last_error || "",
                     text: info.last_error
                       ? info.last_error
                       : `${info.items_today} today · ${fmt.ago(info.last_poll)}` }),
      ]);
    });
    const recent = (watch.recent || []).slice(0, 5).map((r) => el("div", {
      class: "truncate py-0.5 text-xs text-zinc-400",
      text: `${r.source} · ${r.title}`, title: `${r.who} — ${fmt.when(r.first_seen)}` }));
    return el("section", { class: "card" }, [
      el("h2", { class: "card-title", text: "Watching" }),
      names.length ? el("div", { class: "divide-y divide-zinc-800/60" }, rows)
                   : el("p", { class: "text-zinc-400", text: "No sources configured yet." }),
      recent.length ? el("div", { class: "mt-3 border-t border-zinc-800 pt-2" }, recent) : null,
    ]);
  }
```
fetch it in `load` and draw it first in the grid:
```js
    const [nodes, telemetry, health, settings, watch] = await Promise.all([
      api("nodes"), api("api/telemetry"), api("health"), api("api/settings"), api("api/watch")]);
    ...
    data.watch = watch;
```
```js
  function draw() {
    const cards = data.nodes.length ? data.nodes.map(nodeCard)
      : [el("div", { class: "card text-zinc-400", text: "No nodes have reported yet." })];
    if (data.watch && Object.keys(data.watch.sources || {}).length) cards.unshift(watchCard(data.watch));
    grid.replaceChildren(...cards);
  }
```
with `data.watch = null` added to the initial `data` object.

In `friday/sentinel/dashboard/views/activity.js` add the prefix and its badge:
```js
const PREFIXES = ["node", "telemetry", "monitor", "audit", "config", "sentinel", "triage",
                  "escalation", "message"];
```
```js
  monitor: cls("badge-indigo"),
```
(inside `BADGE`, next to `audit`), and give the two event types a readable summary in `summary`:
```js
    case "monitor.item": return `${p.source || ""} · ${p.title || ""}`.trim();
    case "monitor.error": return `${p.source || ""}: ${p.message || ""}`.trim();
```

- [ ] **Step 5: Rebuild the CSS and run the tests**

Run: `deploy/build_css.sh && .venv/bin/pytest tests/sentinel -q && .venv/bin/pytest -q`
Expected: all PASS. A `test_css_covers_every_class` failure lists the missing tokens — they are
literals Tailwind's content scan did not see; fix the source and rebuild.

---

### Task 10: Docs and final verification

**Files:**
- Modify: `readme.md`, `deploy/README.md`

- [ ] **Step 1: `deploy/README.md`**

After the Dashboard section add:
````markdown
## Watching mail, calendar and Jira

FRIDAY can watch three surfaces. Everything is off until you configure it and flip the
switch on **Controls**.

```bash
# 1. Jira and personal mail: paste the credentials into Settings → sources in the dashboard.
#    sources.jira.base_url / .email / .api_token      (an Atlassian API token)
#    sources.imap.host / .user / .password            (an app password; there is no SMTP setting)
# 2. Work mail and calendar: create a Google Cloud OAuth client (Desktop app), put the id and
#    secret in Settings → sources, then grant access once from a machine with a browser:
.venv/bin/python -m friday.sentinel google-auth
#    On a headless Pi, run it on your laptop with --print-only and paste the token into Settings.
```

What each capability may do is fixed in code, not configuration: mail is read-and-draft
(IMAP drafts are written with `APPEND`, and **no SMTP credential exists anywhere**, so sending
is impossible), the calendar may only modify events FRIDAY itself created, and Jira is GET-only.
Nothing is scored or escalated yet — that is the next sub-project. The **Watching** card on
Overview shows each source's state, and every item appears in Activity as `monitor.item`.

`friday-sentinel google-auth --revoke` forgets the token; the sources then report `needs reauth`.
````
In the API table add:
```
| `GET /api/watch` | session | per-source state and the last ten items seen |
```

- [ ] **Step 2: `readme.md`**

- In **What the sentinel does**, add a bullet:
  "**Watching.** Least-privilege monitors poll work mail (Gmail API), personal mail (IMAP), Google Calendar and Jira, normalise what they find into one item shape and put it on the bus. Mail can be read and drafted but never sent — the IMAP path holds no SMTP credential at all — the calendar may only change events FRIDAY created, and Jira is read-only by construction."
- After the **Text assistant** subsection add:
  ```markdown
  ### Watching

  `friday/sentinel/sources/` holds one capability object per surface, each exposing only the
  verbs it is allowed to use: `GmailAccount` (read + draft), `ImapAccount` (read + draft by
  `APPEND`), `CalendarGuarded` (read, create, and modify only what it stamped as its own), and
  `JiraReadOnly` (one `_get`, no write method to call). Two tests enforce the promises rather
  than trusting them: no module under `friday/` may import `smtplib`, and no executable string
  in `sources/` may name a send endpoint.

  `friday/sentinel/watch.py` polls each source on its own interval, honours the
  `controls.monitors.*` switches live, deduplicates through `watch_items` (the INSERT is the
  dedupe) and publishes `monitor.item`. A source that fails goes `degraded` and backs off; a
  revoked credential goes `needs_reauth` and stops retrying, because only a human can fix it.
  Credentials live in the vault under `sources.*`; Google consent is a one-time
  `friday-sentinel google-auth`.
  ```
- In **Sentinel API**, add the `/api/watch` row from Step 1.
- In **Repository Structure**, under `sentinel/` add:
  `│       ├── sources/               # least-privilege capabilities: gmail, imap, calendar, jira`
  `│       ├── watch.py               # Monitor protocol, WatchRunner, the three monitors`
- In **Tests**, extend the sentence with: "the source capabilities against fake Google, Jira and
  IMAP servers (HTML-only mail, RFC 2047 headers, all-day events, oversized bodies), the two
  send guardrails, and the watch runner's dedupe, switch, backoff and degrade/recover paths".

- [ ] **Step 3: Verify the docs against the source**

Run: `grep -c "api/watch\|google-auth\|sources/" readme.md deploy/README.md`
Expected: at least 4 in `readme.md`, 3 in `deploy/README.md`.
Run: `ls friday/sentinel/sources/ friday/sentinel/watch.py`
Expected: every documented path exists.

- [ ] **Step 4: Final verification**

```bash
.venv/bin/pytest -q                                  # all green, 0 skipped on the Mac
.venv/bin/pytest tests/test_boundaries.py -q
deploy/build_css.sh && git status --short friday/sentinel/dashboard/tailwind.css   # no drift
git status --short | wc -l
```

- [ ] **Step 5: Live pass against the real accounts**

This is the first sub-project whose value cannot be seen without real credentials.

1. Put the Jira and IMAP credentials in Settings → sources; run `google-auth` for the Google pair.
2. Turn on all three switches in Controls.
3. Within a poll interval, Activity shows `monitor.item` rows from each configured source, and
   the Watching card shows `watching` with a recent `last_poll`.
4. **Check the Jira default JQL is accepted.** `text ~ currentUser()` is not valid on every
   deployment. If the card shows `degraded — invalid JQL: …`, replace that clause with
   `text ~ "<your display name>"` in Settings → sources and amend the default in
   `friday/sentinel/sources/jira.py`.
5. Restart the daemon: no item is re-published (the dedupe survives a restart).
6. `friday-sentinel google-auth --revoke`, wait one interval: email and calendar show
   `needs reauth` with the remedy, Jira keeps working. Re-run consent and watch them recover.
7. Confirm no mail was sent, nothing was marked read (IMAP uses `BODY.PEEK`), and no calendar
   event changed.

Leave everything uncommitted.
