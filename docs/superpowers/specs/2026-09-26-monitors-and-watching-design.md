# Sub-project 4a: Monitors and watching

**Roadmap:** `docs/superpowers/specs/2026-09-21-sentinel-evolution-roadmap.md` (pillar C, first half)
**Builds on:** sub-projects 1–3, all implemented.
**Followed by:** sub-project 4b — triage scoring, the policy engine, quiet hours, VIP handling,
digests and escalation commands. 4a deliberately stops before anything *decides* what to do.

## 1. Goal

Give FRIDAY safe, least-privilege eyes on the three surfaces Vince works in — work mail
(Google Workspace), personal mail (IMAP), calendar and Jira — and put everything they see on
the event bus in one normalised shape, deduplicated and visible in the dashboard. Nothing is
scored, nothing calls anyone, nothing is suppressed: this sub-project is only about *seeing*,
and about the guarantees that make it safe to look.

The measure of success: with the three switches on, the Activity stream shows real items from
all three sources within a poll interval, a restart re-reads nothing it has already seen, a
revoked credential degrades one source and nothing else, and no code path anywhere can send
mail.

### Decisions (from brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Mail accounts | Both: work via Gmail API, personal via IMAP | Workspace usually disables app passwords; personal keeps the stronger guarantee |
| Transport | Raw REST over `aiohttp` + stdlib `imaplib` | Zero new dependencies on the Pi; least privilege becomes structural rather than a promise |
| Where triage runs (4b) | Monitors publish `monitor.item`; a handler scores it later | Polling stays fast; the durable queue absorbs bursts and retries a failed scoring |
| Jira scope | `assignee = currentUser()` + priority/blocked, JQL editable in the vault | Matches how Vince actually gets blocked; tunable without a code change |
| Dedupe and pile | One `watch_items` table keyed `source:external_id` | Serves dedupe, the dashboard's record, and (in 4b) the digest pile |
| Split | 4a watching, 4b deciding | The roadmap's pillar C is two plans' worth; the seam between I/O and decision logic is clean |

### Non-goals

- Scoring, urgency, VIP lists, quiet hours, digests, calls, WhatsApp. All 4b.
- Sending mail, replying, deleting, archiving, or modifying calendar events FRIDAY did not create.
- Watching anything else (Slack, GitHub, SMS). The `Monitor` protocol is the seam for those.
- Real-time push (Gmail watch/Pub-Sub, IMAP IDLE). Polling on an interval is enough for a
  triage loop whose fastest escalation is a phone call; push is a later optimisation.
- Replacing the desktop's `sentry_personal.py`. That stays as the Mac's own EventKit/Apple Mail
  tooling for the HUD; this is the sentinel's platform-neutral equivalent.

## 2. Capabilities (`friday/sentinel/sources/`)

```
friday/sentinel/sources/
├── __init__.py     re-exports the capability classes and shapes
├── base.py         WatchItem, MailMessage, CalendarEvent, JiraIssue, PermissionDenied,
│                   SourceError, AuthExpired, MailAccount protocol
├── oauth.py        GoogleOAuth: refresh_token -> access token, cached until expiry
├── google.py       GmailAccount, CalendarGuarded
├── imap.py         ImapAccount
└── jira.py         JiraReadOnly
```

Each capability exposes only the permitted verbs; there is no general-purpose "call the API"
method on any of them. Where a guarantee is structural the spec says so, and where it is only a
scope promise the spec says that too.

### 2.1 Shapes (`base.py`)

```python
@dataclass(frozen=True)
class WatchItem:
    source: str               # "email" | "calendar" | "jira"
    external_id: str          # stable per source: IMAP UID, Gmail id, event id, issue key
    title: str
    snippet: str              # <= 500 chars, plain text
    who: str                  # sender / organiser / reporter, display form
    url: str                  # deep link a human can open, "" when none
    ts: float                 # when the item happened, not when we saw it
    meta: Mapping[str, Any]   # source-specific extras; JSON-serialisable

class SourceError(RuntimeError):      """A source failed transiently."""
class AuthExpired(SourceError):       """The credential is revoked or expired: stop retrying."""
class PermissionDenied(RuntimeError): """The capability refused the operation by policy."""
```

`MailMessage`, `CalendarEvent` and `JiraIssue` are the richer per-source dataclasses the
capabilities return; monitors flatten them to `WatchItem`.

### 2.2 `GmailAccount` (work mail)

Scopes: `https://www.googleapis.com/auth/gmail.readonly` and `.../gmail.compose`. Methods:

| Method | Call |
|---|---|
| `list_unread(limit)` | `GET /gmail/v1/users/me/messages?q=is:unread -in:chats&maxResults=` |
| `fetch(id)` | `GET /gmail/v1/users/me/messages/{id}?format=full`, taking the first `text/plain` part and truncating it to the snippet limit |
| `create_draft(to, subject, body, thread_id=None)` | `POST /gmail/v1/users/me/drafts` |

The guarantee here is a **scope promise, not a construction**: `gmail.compose` technically
permits `messages/send`, so the protection is (a) the module contains no send path and (b) a
test parses every module in `friday/sentinel/sources/` with `ast`, collects every string
literal that is not a docstring, and fails if any contains `send`. Prose may therefore explain
the guarantee while no executable string can name the endpoint. This is weaker than the IMAP
path and is the price of Workspace; the `gmail.compose` scope is kept deliberately so that
4b's draft replies need no second consent.

### 2.3 `ImapAccount` (personal mail)

Stdlib `imaplib.IMAP4_SSL`, every call in a thread executor with a timeout. Methods:
`list_unread(limit)` (`UID SEARCH UNSEEN`, newest first, never marking as read — `BODY.PEEK`),
`fetch(uid)`, `create_draft(...)` (`APPEND` an `email.message.EmailMessage` to the Drafts folder
with the `\Draft` flag).

The guarantee is **structural**: the registry has no SMTP key of any kind, so there is no
credential with which to send. A test asserts no `smtplib` import anywhere in `friday/`.

### 2.4 `CalendarGuarded`

| Method | Behaviour |
|---|---|
| `list(time_min, time_max)` | `GET /calendar/v3/calendars/primary/events` |
| `create(...)` | `POST`, stamping `extendedProperties.private.created_by = "friday"` |
| `update(id, ...)` / `delete(id)` | `GET` the event first; if the stamp is absent, raise `PermissionDenied` — no write is attempted |
| `in_meeting(now)` | derived from `list`; returns `bool`. 4b's meeting guard uses it; 4a ships and tests it here because it belongs to the capability |

The refusal raises `PermissionDenied` for the caller to handle. Nothing in 4a calls `update`
or `delete` — the monitors only read — so the audit entry that makes a refusal visible in
Activity lands in 4b, with the first caller that can attempt a write.

### 2.5 `JiraReadOnly`

Jira Cloud REST v3, Basic auth (`email:api_token`). One private method, `_get(path, params)`;
there is no `_post`, `_put` or `_delete` on the class, so a future edit that tries to write has
to add a method rather than change an argument. Methods: `search(jql, limit)`,
`issue(key)`, `comments(key, since)`. A test introspects the class and fails if any attribute's
source contains a non-GET verb.

### 2.6 `GoogleOAuth` (`oauth.py`)

Holds `client_id`, `client_secret`, `refresh_token`; exchanges for an access token at
`POST https://oauth2.googleapis.com/token` and caches it until 60 s before expiry. A
`400 invalid_grant` raises `AuthExpired` — the caller parks the source as needing re-consent
instead of hammering Google. One instance is shared by `GmailAccount` and `CalendarGuarded`.

## 3. Monitors (`friday/sentinel/watch.py`)

```python
class Monitor(Protocol):
    name: str                                   # "email" | "calendar" | "jira"
    interval_key: str                           # registry key holding its poll interval
    async def poll(self, ctx: WatchContext) -> list[WatchItem]: ...
```

`WatchContext` carries `services`, `store`, `logger` and the per-source cursor helpers.

**`WatchRunner`** is what the daemon supervises, one task per monitor, in the same shape as the
existing `TelemetryMonitor`:

1. read `controls.monitors.<name>` — when off, sleep the interval and do nothing else, so the
   dashboard switch takes effect on the next tick without a restart;
2. read the interval from `RuntimeConfig` each tick (live, like the sentinel's other intervals);
3. `poll()` under a timeout; on `AuthExpired` mark the source `needs_reauth` and back off to the
   maximum interval; on `SourceError` mark `degraded` and back off exponentially to the same cap;
   on success clear the state;
4. for each item, call `watch_item_add`; publish
   `Event(type="monitor.item", source=node_id, payload=item.to_dict())` only when it returns
   True. The insert is the dedupe, so a re-poll of the same message produces no second event;
5. publish `monitor.error` with `{source, kind: "auth"|"transient", message}` on failure, once
   per transition rather than once per tick — a source that is down for an hour produces one
   event, not sixty.

### 3.1 The three monitors

- **`EmailMonitor`** — polls every configured account (Gmail and/or IMAP; both are optional and
  it runs with either). Unread messages only. Gmail keeps **no cursor**: `q=is:unread` is
  already the working set and `watch_items` deduplicates it, which also avoids the
  `historyId`-expired failure mode that a history cursor would introduce. IMAP keeps the
  highest UID seen, purely to shorten the `UID SEARCH`.
- **`CalendarMonitor`** — events starting in the next `sources.calendar_horizon_min` (default
  120) minutes, plus any event created or updated since the cursor. This is what later gives 4b
  both the meeting guard and "your 3 o'clock moved".
- **`JiraMonitor`** — runs `sources.jira.jql`, whose default is

  ```
  ((assignee = currentUser() AND statusCategory != Done
    AND (priority in (Highest, High) OR status in (Blocked, "On Hold") OR flagged is not EMPTY))
   OR (text ~ currentUser() AND updated >= -1d))
  ```

  Cursor: the maximum `updated` seen. **`text ~ currentUser()` needs verification against the
  live instance** — Jira's text operator normally wants a literal, and not every deployment
  accepts a function there. The first live pass checks it; if Atlassian rejects it the clause
  becomes `text ~ "<the configured account's display name>"` and the default is amended. This
  is exactly why the JQL is vault-editable.

### 3.2 Normalisation

Everything becomes a `WatchItem` before it reaches the bus, so 4b's triage sees one shape:

| Source | title | who | url | meta |
|---|---|---|---|---|
| email | subject | `From` display name and address | Gmail permalink, or `""` for IMAP | `{account, thread_id, unread_count}` |
| calendar | event summary | organiser | `htmlLink` | `{start, end, attendees, created_by_friday}` |
| jira | `KEY: summary` | reporter or last commenter | browse URL | `{key, priority, status, is_comment}` |

## 4. Storage (schema v5)

```sql
CREATE TABLE watch_items (
  id TEXT PRIMARY KEY,               -- "<source>:<external_id>"
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL,
  who TEXT NOT NULL,
  url TEXT NOT NULL,
  ts REAL NOT NULL,                  -- when the item happened
  first_seen REAL NOT NULL,          -- when FRIDAY saw it
  meta TEXT NOT NULL                 -- JSON
);
CREATE INDEX watch_items_seen ON watch_items(first_seen DESC);
CREATE INDEX watch_items_source_ts ON watch_items(source, ts DESC);
```

`Store` / `AsyncStore` gain `watch_item_add(item, first_seen) -> bool` (False when the id already
exists — that Boolean *is* the dedupe), `watch_items_list(source=None, limit=100)`,
`watch_item_get(id)`, `watch_items_prune(before_ts) -> int`, and
`watch_counts(since_ts) -> dict[str, int]` for the dashboard card. Housekeeping prunes rows
older than `sentinel.retention_days`, as it already does for events and telemetry.

Cursors live in the existing `kv` table under `watch.cursor.<source>`; nothing new is needed.

4b extends this table with `urgency`, `category`, `reason`, `decision` and `digest_state` in
schema v6 — the row that was seen becomes the row that was judged.

## 5. Configuration

New registry group `sources`, appended to `GROUP_ORDER` before `controls`:

| Key | Type | Secret | Default |
|---|---|---|---|
| `sources.google.client_id` | str | no | — |
| `sources.google.client_secret` | str | **yes** | — |
| `sources.google.refresh_token` | str | **yes** | — |
| `sources.imap.host` | str | no | — |
| `sources.imap.port` | int | no | 993 |
| `sources.imap.user` | str | no | — |
| `sources.imap.password` | str | **yes** | — |
| `sources.imap.drafts_folder` | str | no | `Drafts` |
| `sources.jira.base_url` | url | no | — |
| `sources.jira.email` | str | no | — |
| `sources.jira.api_token` | str | **yes** | — |
| `sources.jira.jql` | str | no | the default above |
| `sources.email_interval_s` | float | no | 120 (30–3600) |
| `sources.calendar_interval_s` | float | no | 300 (30–3600) |
| `sources.jira_interval_s` | float | no | 300 (30–3600) |
| `sources.calendar_horizon_min` | int | no | 120 (5–1440) |
| `sources.max_backoff_s` | float | no | 900 (60–7200) |

A source is *configured* when its required keys are non-empty; the runner skips an unconfigured
source with a one-line log at boot rather than failing. The existing `controls.monitors.email`,
`.calendar` and `.jira` switches gate them at runtime.

## 6. Consent (`friday-sentinel google-auth`)

A one-time installed-app OAuth flow, CLI-only — an unauthenticated web consent route is exactly
the surface sub-project 1 refused.

```
$ friday-sentinel google-auth
Open this URL, grant access, and paste nothing — the browser will redirect back here:
  https://accounts.google.com/o/oauth2/v2/auth?...&redirect_uri=http://127.0.0.1:8823
Waiting for the redirect…
Refresh token stored in the vault (fingerprint 0f3a…). Gmail and Calendar are ready.
```

It reads `sources.google.client_id` / `.client_secret` from the vault, serves one request on
`127.0.0.1:8823`, exchanges the code with `access_type=offline&prompt=consent`, and writes
`sources.google.refresh_token`. `--print-only` prints the token instead of writing it, for the
case where you run consent on the Mac and paste it into the Pi's dashboard. `--revoke` clears
the stored token. Every path audits `sources.google.consent` with no token material in the detail.

## 7. Dashboard

A **Watching** card on Overview, one row per source:

```
  email     ● needs reauth  Google refused the refresh token — run friday-sentinel google-auth
  calendar  ● watching      last poll 2 min ago      3 items today
  jira      ● degraded      HTTP 502 from Atlassian — retrying in 4 min
```

Status comes from `GET /api/watch` (session only): `{sources: {name: {enabled, configured,
state, last_poll, last_error, items_today}}}` plus `recent: [WatchItem…]` for the last ten.
The card refreshes on `monitor.item` / `monitor.error` from the socket, as the other cards do.

`monitor.item` and `monitor.error` get their own badge colour in Activity (`monitor` → indigo,
errors inherit the rose treatment through `monitor.error`), so the raw feed is legible from day
one without a new view.

## 8. Error handling

- Every HTTP call: `asyncio.timeout` (default 20 s) and an explicit non-2xx → `SourceError`.
- Every IMAP call: in an executor, wrapped in `asyncio.wait_for`; a hung server cannot block the
  loop.
- `AuthExpired` → state `needs_reauth`, backoff pinned at `sources.max_backoff_s`, one
  `monitor.error` event, the dashboard shows the remedy. No retry storm against Google.
- `SourceError` → state `degraded`, exponential backoff capped at `sources.max_backoff_s`.
- A Jira `400` (almost always a bad JQL the user typed) is treated like an auth failure rather
  than a transient one: state `degraded` with the reason `invalid JQL: <Atlassian's message>`,
  backoff pinned at the maximum. Retrying a query Jira has already rejected helps nobody, and
  the dashboard shows the text needed to fix it.
- A capability refusal (`PermissionDenied`) is audited and returned to the caller; it never
  degrades the source, because nothing is broken.
- A malformed item from a source is logged and skipped, never crashing the poll.
- Secrets never appear in a `monitor.error` message, an audit detail, or a log line; error text
  is the HTTP status and reason, not the request.

## 9. Testing

- **Allowlists:** `JiraReadOnly` has no non-GET verb in any method's source; `friday/` contains
  no `smtplib` import; `friday/sentinel/sources/` contains no `messages/send` or `drafts/send`;
  `CalendarGuarded.update`/`delete` on an unstamped event raise `PermissionDenied` without
  issuing a write.
- **Transports:** an `aiohttp` test server stands in for Google and Jira (token refresh, 401 →
  `AuthExpired`, 500 → `SourceError`, pagination); a stub object stands in for `imaplib`. No test
  needs a live credential or network.
- **Monitors:** dedupe (the same item twice yields one row and one event), cursor advance and
  persistence across a restart, the switch flipped off mid-run, error → degraded → recovery,
  one `monitor.error` per transition rather than per tick, and a source that is unconfigured
  being skipped.
- **Storage:** v4 → v5 migration preserving rows; `watch_item_add` returning False on a repeat.
- **API and dashboard:** `/api/watch` auth, shape, and the class-coverage plus module-parse
  checks the dashboard tests already run.
- **Boundary:** unchanged and green — `aiohttp`, `cryptography`, `google-genai` and the stdlib
  are still the whole list.

## 10. Risk

The credentials this sub-project introduces are the most sensitive the system has held: a
Google refresh token and an IMAP password reach a real mailbox. Three things contain that —
the vault encrypts them at rest under `FRIDAY_MASTER_KEY` with the setting key as AAD, the API
never returns them (masked `{set, hint}`), and the capability objects cannot express a send.
The Gmail path's guarantee is the weakest link and is a scope promise rather than a
construction; if that is not good enough later, the answer is to drop Workspace to read-only
and draft only on the IMAP account.

## 11. Implementation phases

1. **Shapes and OAuth** — `base.py`, `oauth.py`, token refresh with `AuthExpired`, fake-server tests.
2. **Capabilities** — `jira.py`, `google.py`, `imap.py` with their allowlist tests.
3. **Storage v5 + registry** — `watch_items`, the `sources` group, housekeeping prune.
4. **Monitors** — `Monitor`, `WatchRunner`, the three monitors, dedupe and cursors, daemon wiring.
5. **Consent CLI** — `friday-sentinel google-auth` with `--print-only` and `--revoke`.
6. **API and dashboard** — `GET /api/watch`, the Watching card, Activity badges.
7. **Docs and verification** — `readme.md`, `deploy/README.md`, full suite, boundary, and a live
   pass against the real accounts.
