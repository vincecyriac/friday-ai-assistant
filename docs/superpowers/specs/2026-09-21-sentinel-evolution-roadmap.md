# FRIDAY sentinel evolution — roadmap

**Date:** 2026-09-21
**Status:** decomposition and roadmap-level architecture approved; sub-project 1 spec at
`2026-09-21-vault-and-dashboard-auth-design.md`
**Builds on:** `2026-09-20-multi-node-scaffold-design.md` (branch `feat/multi-node-scaffold`)

## 1. Goal

Grow `friday.sentinel` from an event logger into the operations hub: a
secure web dashboard with a voice-capable assistant, an encrypted
configuration vault that is the config authority for every node, strictly
least-privileged monitors over email / calendar / Jira with LLM triage and a
deterministic escalation policy, a WhatsApp bridge, and a telephony bridge
through the secondary Android phone. All of it stays cross-platform
(macOS dev, Fedora x86_64, Raspberry Pi OS ARM64) with
`tests/test_boundaries.py` green throughout.

## 2. Decomposition and order

Each sub-project gets its own spec → plan → implementation cycle and must
leave the system working on its own.

| # | Sub-project | Delivers | Depends on |
|---|---|---|---|
| 1 | **Vault + settings API + dashboard auth** | `FRIDAY_MASTER_KEY`, AES-GCM vault, schema v2, settings registry, `RuntimeConfig`, single-user login, node tokens, `/api/settings`, `/config` pull for the desktop, CLI, login/settings/tokens pages | scaffold |
| 2 | **Dashboard UI + text assistant** | node/telemetry cards, audit stream over `/ws`, controls (call mode, DND, monitor toggles), text chat with sentinel-side tools — the tool loop triage and the call agent reuse | 1 |
| 3 | **Live engine extraction + browser voice** | `friday.core.live`: Gemini Live session manager with pluggable audio transports (PyAudio, browser WebSocket, ALSA); the hub uses it; the dashboard gets the orb and voice. May slide to just before 6 | 2 |
| 4 | **Monitors + triage + escalation** | `Monitor` protocol, `EmailReadDraft`, `CalendarGuarded`, `JiraReadOnly`, meeting guard, `TriageHandler`, policy engine, digests | 1, 2 |
| 5 | **WhatsApp bridge** | whatsmeow/Baileys sidecar on the server paired to the secondary phone; two-way text, voice-note transcription → task extraction, briefings, `[CRITICAL]` fallback | 1, 4 |
| 6 | **Telephony bridge** | ADB call control, USB audio loopback → ALSA, `TelephonyBridge` + `CallSession` on the Live engine, Vince mode vs external-caller script, MOM via WhatsApp | 3, 4, 5 |

## 3. Decisions made in brainstorming

| Decision | Choice | Why |
|---|---|---|
| Call audio path | Physical loopback: USB-C audio adapter from the phone into a USB sound card on the server, exposed as ALSA devices | Android exposes no in-call audio API; a VoIP number would not be the secondary phone; ring-only drops most of pillar 4 |
| Call control channel | ADB over Wi-Fi from the server (`KEYCODE_CALL`, `KEYCODE_ENDCALL`, `am start -a CALL`, `dumpsys telephony.registry`) | Termux:API can dial and SMS but cannot answer a call |
| WhatsApp transport | Unofficial sidecar (whatsmeow or Baileys) on the server, paired to the secondary phone's number | Cloud API needs business verification and template messages outside a 24 h window; a ban hits the secondary number only. Sidecar speaks `/events` + `/ws` — outside the Python boundary |
| Web assistant | Text chat with tools **and** browser voice via an extracted Live engine | The call agent needs the same engine; extraction shrinks `hub.py` |
| Config authority | Sentinel vault; the desktop pulls `GET /config?scope=desktop` and falls back to `.env` | One place to rotate keys; the dashboard configures everything |
| Vault crypto | `cryptography` AES-GCM, key from `FRIDAY_MASTER_KEY` via HKDF, AAD = setting key | Stdlib has no AEAD; AAD binding prevents ciphertext swapping between slots |
| Password hashing | `hashlib.scrypt` (stdlib) | No extra dependency |
| Bootstrap | CLI only (`friday-sentinel user set-password`, `token create`); no setup page | An unauthenticated setup route is an attack surface |
| Node auth | Vault-managed node tokens, hashed at rest; `FRIDAY_SENTINEL_TOKEN` no longer read by the sentinel | `.env` holds host bootstrap only |
| Dashboard frontend | Vanilla JS + CSS, no build step, `hud-*` design language | Nothing to compile on the Pi; matches the desktop GUI |
| Policy | Deterministic engine over the LLM's urgency score | Testable; the model never decides to ring you at 3 am on its own |
| Digest channel | WhatsApp | As specified; until sub-project 5 exists digests show in the dashboard |

## 4. Feasibility flags carried forward

- **USB-C call audio** — verify on the actual secondary phone (before sub-project 6) that
  call audio routes to a USB-C audio adapter, not only a 3.5 mm headset. Five-minute
  spike with a wired headset and a test call. Go/no-go for the audio design.
- **ADB wireless debugging** must be enabled on the phone; it re-pairs after reboots on
  some Android versions. Sub-project 6 must handle "ADB unreachable" as a degraded state
  (ring-and-notify only), not a crash.
- **Workspace Gmail** may block app passwords; if so the email capability uses the Gmail
  API whose `gmail.compose` scope also permits send, and least privilege there is
  code-level only. Sub-project 4's first question.
- **Termux on Android** cannot install `pydantic-core` (no bionic wheels), so nothing on
  the phone imports `friday.core`; the phone speaks the HTTP/WS API only.

## 5. Architecture by pillar

### A. Vault (sub-project 1 — full detail in its spec)

`.env` keeps host bootstrap only: `FRIDAY_DATA_DIR`, `FRIDAY_NODE_ID`,
`FRIDAY_SENTINEL_BIND`, `FRIDAY_MASTER_KEY`, `FRIDAY_LOG_LEVEL`,
`FRIDAY_DB_SYNCHRONOUS`, `FRIDAY_TRUSTED_PROXY`, and the operational intervals until
sub-project 2 makes monitors live-reconfigurable. Everything else lives in the
`settings` table, typed by a code registry, secrets AES-GCM encrypted with the setting
key as AAD. Precedence per key: vault → legacy `.env` (read-only, labelled) → default.
`RuntimeConfig` fronts it and publishes `config.changed` on the bus. Schema v2 adds
`settings`, `users`, `sessions`, `node_tokens`, `audit`.

### B. Dashboard auth and API (sub-project 1 — full detail in its spec)

One principal resolver: session cookie → `User` (full access); bearer node token →
`Node` (events, ws, scoped config, health); nothing → health, login, static. Cookie
`HttpOnly; SameSite=Lax; Secure` when HTTPS (direct, or `X-Forwarded-Proto` when
`FRIDAY_TRUSTED_PROXY=1`). CSRF: `SameSite=Lax` + mandatory `X-FRIDAY-Client:
dashboard` header on state-changing `/api/*`. Login lockout 5 failures / 60 s per IP.
Dashboard is static files served by aiohttp at `/`, relative URLs only.

### C. Monitors, triage, escalation (sub-project 4 — roadmap depth)

- `Monitor` protocol: `name`, `interval_s`, `async poll(ctx) -> list[Event]`; supervised;
  `controls.monitors.<name>` is a live switch via `config.changed`.
- Capabilities are objects with only the allowed verbs; a test introspects each class and
  asserts the allowlist:
  - `EmailReadDraft`: `list_unread`, `fetch`, `create_draft`. IMAP + app password preferred:
    drafts by `APPEND` to Drafts, **no SMTP credential stored** — sending is impossible by
    construction. Gmail API fallback documented with its weaker guarantee.
  - `CalendarGuarded`: `list`, `create` (stamps `extendedProperties.private.created_by=friday`),
    `update`/`delete` refuse anything unstamped with an audited `PermissionDenied`.
  - `JiraReadOnly`: transport rejects any HTTP method but `GET`.
  - Meeting guard: `in_meeting()` from `CalendarGuarded.list` for "now".
- `TriageHandler` → `get_provider("triage").generate()` → `{urgency, reason, category}`.
  The **policy engine** (code) maps `urgency × controls.call_mode × DND × meeting guard ×
  quiet hours` → `command.call.dial` / digest bucket → `command.whatsapp.send`. Every
  decision, including suppressions, is a `triage.decision` event.
- Escalation follow-through: `call.unanswered` (30 s) → `command.whatsapp.send [CRITICAL]`.
  Before bridges exist, commands park in the queue and show in the dashboard.

### D. Telephony bridge (sub-project 6 — roadmap depth)

- **Control:** ADB over Wi-Fi from the server. Dial, answer, hang up, and read ring /
  off-hook / idle state with the caller number. Termux stays for SMS only.
- **Audio:** phone → USB-C audio adapter → USB sound card on the server → ALSA capture and
  playback devices named in the vault (`telephony.audio.capture`, `telephony.audio.playback`).
  Android routes call audio to a wired headset when present; no app on the phone.
- **`TelephonyBridge`** (implements `Bridge`): subscribes to `command.call.dial|hangup`;
  drives ADB with bounded timeouts off the loop; publishes `call.ringing {number}`,
  `call.answered`, `call.unanswered`, `call.ended {duration}`; on connect starts a
  **`CallSession`** on the shared Live engine with an `AlsaTransport` and a persona chosen by
  caller: whitelisted primary number → **Vince mode** (tools: status, tasks, calendar create,
  email draft); anyone else → **external-caller script** (fixed greeting that never reveals
  the primary number, "would you like to leave a message?", answer in the caller's language
  — native to Gemini Live). Inbound auto-answer after N rings only when `controls.call_mode`
  permits. On `call.ended`: transcript → MOM (or caller-message summary + audio) →
  `command.whatsapp.send`.
- **Phone-side code:** none for calls. The WhatsApp sidecar runs on the server.

## 6. Cross-cutting constraints (every sub-project)

- `tests/test_boundaries.py` stays green; no macOS module in `core` or `sentinel`.
  Linux-only libraries (e.g. ALSA bindings) go in a `[telephony]` extra and are imported
  lazily inside the bridge, so the boundary test still passes on macOS.
- SQLite stays WAL; multi-statement changes are explicit `BEGIN`/`COMMIT`; migrations are
  forward-only and numbered.
- Every external call (Google, Jira, ADB, sidecar, phone) is awaited with a timeout and
  runs off the event loop when it blocks; a failing integration degrades that monitor or
  bridge, never the daemon.
- Secrets never appear in logs, events, or API responses; masked as `{set, hint}`.
- No commits by the assistant; the user commits.

## 7. Open items owned by later sub-projects

- Sub-project 2: make intervals and retention vault-managed and live; `/api/events`
  filters; chat tool set.
- Sub-project 3: transport interface for the Live engine; how the hub's 38 tools split
  between desktop-only and sentinel-capable.
- Sub-project 4: IMAP vs Gmail API per account; JQL for "blocker"; quiet hours; digest
  cadence.
- Sub-project 5: whatsmeow (Go) vs Baileys (Node) sidecar; media storage under `data/`.
- Sub-project 6: ALSA library choice (`pyalsaaudio` vs PortAudio via `sounddevice`);
  ring count before auto-answer; ADB reconnection policy.
