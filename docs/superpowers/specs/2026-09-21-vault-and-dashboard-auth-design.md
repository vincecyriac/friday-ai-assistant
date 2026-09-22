# Sub-project 1: encrypted vault, settings API, dashboard auth — design

**Date:** 2026-09-21
**Status:** approved in brainstorming; awaiting implementation plan
**Roadmap:** `2026-09-21-sentinel-evolution-roadmap.md` §5 A–B
**Builds on:** the scaffold on `feat/multi-node-scaffold`

## 1. Goal

Make the sentinel the configuration authority for every node, with secrets encrypted
at rest, a single-user login, vault-managed node tokens, a schema-driven settings API,
and the first three dashboard pages (login, settings, nodes & tokens). The desktop pulls
its LLM configuration from the sentinel and falls back to `.env`.

### Non-goals

- Telemetry cards, audit stream, controls UI, chat — sub-project 2.
- Making intervals/retention live-reconfigurable — sub-project 2.
- Any monitor, bridge, or triage logic.
- Master-key rotation tooling (documented as a follow-up; the format supports it).
- Multi-user, roles, TOTP.

## 2. Bootstrap configuration (`.env`)

`friday.core.config.Settings` gains:

| Field | Env | Default | Notes |
|---|---|---|---|
| `master_key: str \| None` | `FRIDAY_MASTER_KEY` | unset | base64 (standard or urlsafe), ≥ 32 bytes decoded. Required by the sentinel; ignored by the desktop |
| `trusted_proxy: bool` | `FRIDAY_TRUSTED_PROXY` | `false` | honour `X-Forwarded-Proto` / `X-Forwarded-For` |
| `env: Mapping[str, str]` | — | — | the merged env **filtered to keys starting with `FRIDAY_`, `GEMINI_` or `TRIPO_`**, so `RuntimeConfig` can read legacy values without the whole process environment riding along in a Settings object |

`sentinel_token` stays on `Settings` **for the desktop only** (its client credential).
The sentinel no longer reads it for authentication.

`friday.core.config` also gains `override_settings(settings: Settings) -> None`, which
replaces what `get_settings()` returns for the rest of the process (used by the desktop
after pulling from the sentinel), and `CONFIG_FIELD_MAP` + `apply_overrides` (§11).

The sentinel refuses to boot without a valid `FRIDAY_MASTER_KEY`:
`friday-sentinel: configuration error: FRIDAY_MASTER_KEY is not set — run
'friday-sentinel keygen' and add the printed line to .env` → exit 1.

## 3. `friday.core.vault`

```python
class MasterKeyError(ConfigError): ...          # missing / malformed / too short
class VaultError(ValueError): ...               # wrong key, tampered, bad format

class Vault:
    VERSION = "v1"
    @classmethod
    def from_master_key(cls, encoded: str) -> "Vault"
    @staticmethod
    def generate_master_key() -> str            # 32 random bytes, urlsafe base64
    def encrypt(self, key: str, plaintext: str) -> str      # "v1:<b64 nonce>:<b64 ct+tag>"
    def decrypt(self, key: str, token: str) -> str          # VaultError on any failure
    def fingerprint(self) -> str                            # first 8 hex of sha256(derived key)
```

- Derivation: `HKDF(SHA256, length=32, salt=None, info=b"friday-vault-v1")` over the
  decoded master key. `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- Nonce: 12 random bytes per encryption. AAD: `key.encode()` — the **setting key**. A
  token moved to a different setting row fails to decrypt.
- `cryptography` becomes a base dependency in `pyproject.toml` (aarch64 manylinux wheels
  exist; Pi OS Bookworm python3.11 is covered).
- `fingerprint()` is logged at boot so a wrong-key mistake is diagnosable without
  revealing anything.

## 4. Storage schema v2

Migration 2 (forward-only, single transaction, on top of v1):

```sql
CREATE TABLE settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE users (
  username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
  created_at REAL NOT NULL, password_changed_at REAL NOT NULL);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,               -- sha256 hex of the cookie token
  username TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  last_seen REAL NOT NULL, user_agent TEXT, ip TEXT);
CREATE INDEX sessions_expires ON sessions(expires_at);
CREATE TABLE node_tokens (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, last_used REAL, revoked_at REAL);
CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL);
CREATE INDEX audit_ts ON audit(ts DESC);
```

`Store` / `AsyncStore` gain (same names on both):

```python
@dataclass(frozen=True) class SettingRow(key, value, secret: bool, updated_at, updated_by)
@dataclass(frozen=True) class UserRow(username, password_hash, created_at, password_changed_at)
@dataclass(frozen=True) class SessionRow(id, username, created_at, expires_at, last_seen, user_agent, ip)
@dataclass(frozen=True) class NodeTokenRow(id, name, created_at, last_used, revoked_at)   # never the hash
@dataclass(frozen=True) class AuditRow(id, ts, actor, action, target, detail: dict)

setting_get(key) -> SettingRow | None
setting_set(key, value, *, secret, updated_by, ts) -> None            # upsert
setting_delete(key) -> bool
settings_all() -> list[SettingRow]
user_get(username) -> UserRow | None
user_upsert(username, password_hash, ts) -> None
session_create(id, username, ts, expires_at, user_agent, ip) -> None
session_get(id) -> SessionRow | None                                  # includes expired; caller checks
session_touch(id, ts) -> None
session_delete(id) -> bool
sessions_prune(now) -> int
node_token_create(id, name, token_hash, ts) -> None
node_token_by_hash(token_hash) -> NodeTokenRow | None                 # revoked rows excluded
node_token_touch(id, ts) -> None
node_tokens_list() -> list[NodeTokenRow]
node_token_revoke(id, ts) -> bool
audit_append(ts, actor, action, target, detail: dict) -> int
audit_list(limit=100, before_id=None) -> list[AuditRow]               # newest first
```

Expired sessions are removed by `sessions_prune`, which `Housekeeping` calls alongside
`prune()` (§10). Audit rows are not pruned in this sub-project.

## 5. Settings registry (`friday/sentinel/settings_registry.py`)

```python
@dataclass(frozen=True)
class SettingSpec:
    key: str                     # dotted, e.g. "llm.gemini_api_key"
    type: str                    # "str" | "int" | "float" | "bool" | "enum" | "url" | "list"
    group: str                   # "llm" | "desktop" | "controls"
    description: str
    default: Any = None
    secret: bool = False         # encrypted at rest, masked in responses
    choices: tuple[str, ...] = ()        # enum
    scopes: tuple[str, ...] = ()         # principals that may pull it via /config, e.g. ("desktop",)
    env: str | None = None               # legacy .env fallback variable
    restart_required: bool = False

REGISTRY: tuple[SettingSpec, ...]
def spec_for(key) -> SettingSpec            # KeyError → unknown setting
def validate(spec, value) -> Any            # coerces; SettingValidationError with the key in the message
def schema() -> list[dict]                  # JSON for GET /api/settings/schema (no values)
```

Initial registry:

| key | type | secret | scopes | env | default |
|---|---|---|---|---|---|
| `llm.gemini_api_key` | str | ✓ | desktop | `GEMINI_API_KEY` | — |
| `llm.routes.live` | str | | desktop | `FRIDAY_LLM_LIVE` (`GEMINI_MODEL` alias handled by config) | `gemini:gemini-3.1-flash-live-preview` |
| `llm.routes.agent_os` | str | | desktop | `FRIDAY_LLM_AGENT_OS` | `gemini:gemini-3.8-flash` |
| `llm.routes.agent_spatial` | str | | desktop | `FRIDAY_LLM_AGENT_SPATIAL` | `gemini:gemini-3.8-flash` |
| `llm.routes.widget` | str | | desktop | `FRIDAY_LLM_WIDGET` | `gemini:gemini-3.7-flash` |
| `llm.routes.triage` | str | | — | `FRIDAY_LLM_TRIAGE` | `gemini:gemini-3.7-flash` |
| `llm.tripo_api_key` | str | ✓ | desktop | `TRIPO_API_KEY` | — |
| `desktop.voice` | str | | desktop | `FRIDAY_VOICE` | `Aoede` |
| `controls.call_mode` | enum `always\|urgent_only\|mute` | | — | — | `urgent_only` |
| `controls.dnd` | bool | | — | — | `false` |

Route values are validated with `parse_route`. Later sub-projects append groups
(`jira.*`, `google.<account>.*`, `whatsapp.*`, `telephony.*`, `controls.monitors.*`).

## 6. `RuntimeConfig` (`friday/sentinel/runtime_config.py`)

```python
class RuntimeConfig:
    def __init__(self, store: AsyncStore, vault: Vault, settings: Settings, bus: EventBus | None = None)
    async def load(self) -> None                       # read all rows once; decrypt lazily on get
    def get(self, key: str) -> Any                     # vault → env → default; typed
    def source(self, key: str) -> str                  # "vault" | "env" | "default"
    async def set_many(self, updates: Mapping[str, Any], *, actor: str) -> None
    async def unset(self, key: str, *, actor: str) -> None
    def view_for_user(self) -> list[dict]              # every spec + {value|set,hint} + source
    def view_for_scope(self, scope: str) -> dict[str, Any]   # decrypted values whose spec.scopes includes scope
```

- `set_many` validates **every** key first (unknown key or bad value → `SettingValidationError`
  listing all offenders; nothing written), then writes each row (secrets via
  `vault.encrypt(key, str(value))`), appends one `settings.update` audit row per key with
  `detail={"source": "vault"}` (never the value; for secrets `{"secret": true}`), refreshes
  the cache, and publishes `Event(type="config.changed", source=node_id,
  payload={"keys": [...]})`.
- Legacy fallback (`source == "env"`): `settings.env[spec.env]` when set. Two special cases
  live in one helper, `legacy_value(spec, settings)`: `llm.routes.live` also honours
  `GEMINI_MODEL` (as `gemini:<model>`), and route values are normalised through
  `parse_route` so a malformed legacy value is reported, not silently used.
- `get` for a secret decrypts on demand; a `VaultError` (wrong master key for an old row)
  is logged once per key and treated as unset — the dashboard shows "undecryptable
  (master key changed?)".
- Masked secret view: `{"set": True, "hint": <last 4 chars of plaintext>}` — the hint is
  only computed for values ≥ 12 chars, else `hint: ""`.

## 7. Auth (`friday/sentinel/auth.py`)

```python
def hash_password(password: str) -> str            # "scrypt$32768$8$1$<b64 salt>$<b64 dk>"
def verify_password(password: str, stored: str) -> bool     # hmac.compare_digest

@dataclass(frozen=True) class User: username: str
@dataclass(frozen=True) class Node: id: str; name: str
Principal = User | Node

def new_token(prefix: str = "") -> str             # prefix + 32 urlsafe-base64 random bytes
def token_hash(token: str) -> str                  # sha256 hex

class SessionManager:
    def __init__(self, store: AsyncStore, ttl_s: float = 30 * 86400)
    async def create(self, username, *, user_agent, ip) -> str          # returns cookie token
    async def resolve(self, token) -> User | None                        # expiry checked; touches last_seen
    async def revoke(self, token) -> None

class NodeTokens:
    def __init__(self, store: AsyncStore)
    async def create(self, name) -> tuple[str, str]                      # (id, plaintext "fn_…")
    async def resolve(self, token) -> Node | None                         # touches last_used
    async def revoke(self, id) -> bool
    async def list(self) -> list[NodeTokenRow]

class LoginLimiter:
    def __init__(self, max_failures=5, lockout_s=60.0)
    def allowed(self, ip) -> bool
    def record_failure(self, ip) -> None
    def reset(self, ip) -> None

async def resolve_principal(request) -> Principal | None    # cookie → session; else Bearer → node token
def client_ip(request, trusted_proxy: bool) -> str          # X-Forwarded-For only when trusted
def is_https(request, trusted_proxy: bool) -> bool          # request.secure, or X-Forwarded-Proto when trusted
```

scrypt parameters `n=2**15, r=8, p=1, dklen=32, maxmem=64 MiB`; `verify_password`
re-derives with the stored parameters so they can be raised later.

## 8. Web layer (`friday/sentinel/web.py`), composed into `create_app`

App keys added: `VAULT`, `CONFIG` (RuntimeConfig), `SESSIONS`, `NODE_TOKENS`, `LIMITER`.

### Principals and access

| Route | Principal required |
|---|---|
| `GET /health`, `GET /login`, `/static/*` | none |
| `POST /auth/login` | none (rate-limited) |
| `POST /auth/logout`, `GET /auth/me` | User |
| `GET /api/settings/schema`, `GET/PUT /api/settings`, `/api/tokens*`, `GET /api/audit` | User |
| `GET /config?scope=` | Node (scoped) or User (any scope) |
| `POST /events`, `GET /ws` | User or Node |
| `GET /nodes`, `GET /telemetry` | User or Node |

Unauthenticated → `401 {"error": "authentication required"}` for `/api/*` and node
routes; `/` and `/settings` etc. redirect to `/login` (302) for browsers.

### Endpoints

- `POST /auth/login` body `{"username","password"}`. Wrong → `401` (+ limiter record;
  audit `login.failed`). Locked out → `429 {"error":"too many attempts","retry_after":N}`.
  Success → `204`, `Set-Cookie: friday_session=<token>; HttpOnly; SameSite=Lax; Path=/;
  Max-Age=2592000` + `; Secure` when `is_https`. Audit `login.ok`.
- `POST /auth/logout` → `204`, cookie cleared, session row deleted. Audit `logout`.
- `GET /auth/me` → `{"username","expires_at"}`.
- CSRF: every `POST/PUT/DELETE` under `/auth/*` and `/api/*` requires header
  `X-FRIDAY-Client: dashboard`, else `403 {"error":"missing client header"}`. If an
  `Origin` header is present it must match the request `Host` (with proxy awareness), else
  `403`.
- `GET /api/settings/schema` → `{"groups": [{"name", "keys": [spec…]}]}`.
- `GET /api/settings` → `{"values": [{"key","value"|"set","hint","source","secret"}]}`.
- `PUT /api/settings` body `{key: value, …}`; `null` unsets. `204` on success;
  `400 {"error": "...", "invalid": {key: message}}` on any bad key (nothing written).
- `GET /api/tokens` → `[{"id","name","created_at","last_used","revoked_at"}]`.
- `POST /api/tokens` body `{"name"}` → `201 {"id","name","token"}` (plaintext once). Audit `token.create`.
- `DELETE /api/tokens/{id}` → `204` / `404`. Audit `token.revoke`.
- `GET /api/audit?limit=50&before=<id>` → `[{"id","ts","actor","action","target","detail"}]`.
- `GET /config?scope=desktop` → `{"scope","values": {key: value}, "generated_at"}`;
  unknown scope → `400`; audit `config.pull` with `actor=node:<name>` and the key list.
- Node routes: `POST /events` and `GET /ws` accept a User cookie **or** a Node bearer.
  `?token=` on `/ws` remains for non-browser clients.

### Static dashboard

`friday/sentinel/dashboard/` → `index.html`, `app.js`, `style.css`; served at
`/` (app shell), `/login` (same shell, login view), `/static/*`. `index.html` loads
`static/app.js` with a relative path. The shell:

- **Login view** — username/password form; `204` → loads `/auth/me` and switches to
  Settings. `429` shows the retry time.
- **Settings view** — groups from the schema; text/number/enum/bool inputs; secrets are
  write-only password inputs showing "set (…7f2a)" / "not set" with a source badge
  (`vault` / `env` / `default`); Save issues one `PUT` with only changed keys; per-key
  errors rendered inline from `invalid`.
- **Nodes & tokens view** — table from `/api/tokens`, create form that shows the plaintext
  once with a copy button, revoke buttons; `/nodes` heartbeat table underneath.
- A `fetch` wrapper adds `X-FRIDAY-Client: dashboard`, uses `credentials: "same-origin"`,
  and routes any `401` to the login view.
- Styling reuses the desktop's `hud-*` token vocabulary (copied, not imported — the
  sentinel never depends on `friday.desktop`).

## 9. CLI (`friday/sentinel/cli.py`; `__main__` delegates to it)

```
friday-sentinel [run]                         start the daemon (default)
friday-sentinel keygen                        print "FRIDAY_MASTER_KEY=<new key>"
friday-sentinel user set-password NAME        prompt twice (getpass) or --password-stdin
friday-sentinel token create NAME             print the plaintext token once
friday-sentinel token list
friday-sentinel token revoke ID
```

CLI commands open the store synchronously (`Store.open`) — WAL + `busy_timeout` make
this safe while the daemon runs — audit with `actor="cli"`, and exit 1 with one line on
`ConfigError`. `keygen` needs no store.

## 10. Daemon wiring

Boot order gains, after the store opens: `Vault.from_master_key(settings.master_key)`
(`MasterKeyError` → exit 1 via `ConfigError`), log the fingerprint, `RuntimeConfig.load()`,
construct `SessionManager`, `NodeTokens`, `LoginLimiter`, and pass all of them to
`ApiServer`. `Housekeeping` also calls `sessions_prune`. If `users` is empty at boot, log a
WARNING with the `user set-password` command; the daemon still runs (node traffic works).

## 11. Desktop pull

- `SentinelClient.fetch_config(scope="desktop") -> dict | None` — `GET /config`, bearer,
  5 s timeout, `None` on any failure (same warn-once logging as `post`).
- `friday/core/config.py`: `CONFIG_FIELD_MAP` (registry key → Settings field) and
  `apply_overrides(settings, values) -> Settings` via `dataclasses.replace`
  (`llm.routes.*` merge into `llm_routes`).
- `friday/desktop/config_pull.py`: `async def pull_or_cached(settings, fetch, cache_path)
  -> tuple[Settings, str]` returning the overlaid settings and a source label
  (`"sentinel"`, `"cache"`, `"env"`). On success writes `cache_path` (JSON, mode `0600`);
  on failure reads it if present; else returns the input unchanged. Pure enough to test
  with a fake `fetch`.
- `hub.run_friday()` calls it first when `settings.sentinel_url` is set, then
  `friday.core.config.override_settings(new)` so `get_settings()` returns the overlaid
  object process-wide, and resolves `MODEL_ID` / `LIVE_VOICE` **after** that. `agents.TIERS`
  store a `role`, and `run_agent` resolves the model per call; `widget_generator` resolves
  per call. `log_info` reports the config source at boot.

## 12. Error handling

| Situation | Behaviour |
|---|---|
| `FRIDAY_MASTER_KEY` missing/short on the sentinel | exit 1 with the keygen hint |
| A stored secret fails to decrypt | treated as unset; logged once; dashboard flags it |
| Login wrong password | 401, audited, limiter counts |
| Locked out | 429 with `retry_after` |
| Missing CSRF header / bad Origin | 403 |
| Unknown or invalid setting in PUT | 400 listing every bad key; nothing written |
| Revoked/unknown node token | 401 |
| `/config` with unknown scope | 400 |
| No users exist | daemon runs; every login 401; boot WARNING |
| Desktop cannot reach sentinel | uses cached config, else `.env`; logs the source |

## 13. Testing

- `tests/core/test_vault.py` — keygen length/format; roundtrip; wrong key; tampered
  ciphertext; AAD swap between keys; malformed token; short master key → `MasterKeyError`.
- `tests/core/test_storage_v2.py` — a v1 database (created by the v1 migration only)
  upgrades to v2 with its rows intact; settings CRUD; user upsert; session create/get/
  touch/delete/prune; node token create/by_hash/revoke (revoked excluded); audit ordering,
  limit, `before_id`.
- `tests/core/test_config.py` — `master_key`, `trusted_proxy`, `env` mapping;
  `apply_overrides` maps every registry key and merges routes.
- `tests/sentinel/test_settings_registry.py` — validation per type incl. enum, url, list,
  route; unknown key; `schema()` carries no values.
- `tests/sentinel/test_runtime_config.py` — precedence vault > env > default with sources;
  all-or-nothing `set_many`; secret row is `v1:`-prefixed at rest and never appears in the
  audit detail; masked view; scoped view excludes non-desktop keys; `config.changed`
  published; unset; undecryptable secret handled.
- `tests/sentinel/test_auth.py` — hash/verify incl. wrong password and parameter
  round-trip; session expiry/revoke; node token revoke; limiter lockout and reset.
- `tests/sentinel/test_web.py` (aiohttp test client) — login 401/204/cookie flags/429;
  `Secure` set only when HTTPS or trusted proxy header; `/auth/me`; logout clears; CSRF
  header enforced; schema/get/put incl. 400 listing; secrets masked; tokens create/list/
  revoke and revoked token → 401; audit rows for each action; `/config` node scoped /
  user / none / bad scope; `/` redirects to `/login`; static served; `/ws` accepts cookie.
- `tests/sentinel/test_api.py` — `/events` without principal → 401 (replaces the
  open-when-no-token test); node token works.
- `tests/sentinel/test_daemon.py` — settings fixture carries a master key; boot without
  one raises `ConfigError`; users-empty warning logged.
- `tests/sentinel/test_cli.py` — `keygen` prints a valid key; `user set-password
  --password-stdin` creates a verifiable user; `token create/list/revoke` round-trip.
- `tests/desktop/test_sentinel_client.py` — `fetch_config` 200/401/unreachable.
- `tests/desktop/test_config_pull.py` — sentinel success writes cache with mode 0600;
  failure reads cache; neither → env; source labels.
- `tests/test_boundaries.py` — unchanged and green (`cryptography` is not poisoned).

## 14. Implementation phases

1. **core** — `Settings` fields; `vault.py`; storage migration 2 + methods; `apply_overrides`.
2. **sentinel foundations** — settings registry; `RuntimeConfig`; `auth.py`.
3. **web** — `web.py` routes; principal resolution; node routes switch to principals;
   `/config`; static serving; compose in `create_app`/`ApiServer`.
4. **cli + daemon** — `cli.py`; `__main__` delegates; daemon wiring; housekeeping prune.
5. **dashboard** — `index.html`, `app.js`, `style.css` (login, settings, tokens).
6. **desktop** — `fetch_config`; `config_pull.py`; hub/agents/widget_generator lazy
   resolution.
7. **docs + verification** — `.env.template` (bootstrap-only + `FRIDAY_MASTER_KEY`),
   `deploy/README.md` (keygen, set-password, token create), `readme.md`; full suite; manual:
   keygen → set-password → token create → login in a browser → paste Gemini key → desktop
   boots with "config source: sentinel".
