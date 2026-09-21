# FRIDAY multi-node scaffold — design

**Date:** 2026-09-20
**Status:** approved in brainstorming; awaiting implementation plan

## 1. Goal

Turn FRIDAY from a single macOS desktop process into a distributed assistant
with two node types sharing one codebase:

- **desktop** — the existing macOS voice/HUD app (unchanged in behaviour).
- **sentinel** — a new 24/7 headless daemon that runs identically on macOS
  (development, `launchd`), Fedora x86_64 (first production, `systemd`) and
  Raspberry Pi OS ARM64 (later production, `systemd`).

Everything shared lives in `friday.core`. Nothing in `friday.core` or
`friday.sentinel` may import a macOS-only module; a test enforces this.

### Non-goals for this pass

- No LLM triage logic. The handler seam exists; the prompt/policy does not.
- No concrete bridges (Termux telephony, WhatsApp, cloud voice). The `Bridge`
  protocol, registry and lifecycle ship; implementations do not.
- No decomposition of `hub.py` beyond what `friday.core` needs. It relocates
  intact.
- Only the Gemini LLM provider ships. The provider interface and per-role
  routing ship so a second adapter is additive.
- No CI workflow.

## 2. Decisions made in brainstorming

| Decision | Choice |
|---|---|
| Hub refactor depth | Relocate intact; extract only shared concerns |
| Node transport | HTTP + WebSocket served by the sentinel (aiohttp), localhost bind, Tailscale Serve for remote |
| LLM providers | Gemini only; factory + routing abstraction ships |
| Package layout | Single `friday/` namespace with `core`, `desktop`, `sentinel` sub-packages |
| Python floor | 3.11 (Raspberry Pi OS Bookworm) |
| Packaging | One `pyproject.toml`, extras `desktop`, `sentinel`, `dev` |
| Runtime state | `FRIDAY_DATA_DIR`, default `<repo>/data/`, git-ignored |
| SQLite sync | WAL + `synchronous=FULL` default; `NORMAL` opt-in |
| Tests | pytest + pytest-asyncio for `core` and `sentinel`; desktop import-smoke only |
| Legacy module names | `sentry_*.py` keep their names inside `desktop/`; `friday_*`/`app_desktop`/`widget_generator_agent` drop their prefixes |

## 3. Repository layout

```
friday-ai-assistant/
├── pyproject.toml
├── .env.template
├── readme.md
├── SVE.md
├── docs/superpowers/specs/
├── friday/
│   ├── __init__.py               # __version__ = "0.1.0"
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── platform.py
│   │   ├── storage.py
│   │   ├── events.py
│   │   ├── telemetry.py
│   │   ├── logging.py
│   │   └── llm/
│   │       ├── __init__.py       # resolve(), get_provider(), gemini_client()
│   │       ├── base.py           # LLMProvider protocol, LLMResponse, ToolCall
│   │       ├── routing.py        # env → Route(provider, model)
│   │       └── gemini.py         # GeminiProvider
│   ├── desktop/
│   │   ├── __init__.py
│   │   ├── __main__.py           # python -m friday.desktop → app.main()
│   │   ├── app.py                # ← app_desktop.py
│   │   ├── hub.py                # ← friday_hub.py
│   │   ├── agents.py             # ← friday_agents.py
│   │   ├── widget_generator.py   # ← widget_generator_agent.py
│   │   ├── asset_generator.py    # ← services/asset_generator.py
│   │   ├── sentry_vision.py      # ← unchanged names
│   │   ├── sentry_action.py
│   │   ├── sentry_exec.py
│   │   ├── sentry_recognition.py
│   │   ├── sentry_scene.py
│   │   ├── sentry_personal.py
│   │   ├── sentry_web.py
│   │   ├── sentinel_client.py    # new
│   │   ├── models/               # face_detection_yunet.onnx, face_recognition_sface.onnx
│   │   └── web_gui/              # ← web_gui/
│   └── sentinel/
│       ├── __init__.py
│       ├── __main__.py           # python -m friday.sentinel
│       ├── daemon.py
│       ├── bus.py
│       ├── handlers.py
│       ├── api.py
│       ├── monitors.py
│       └── bridges/
│           ├── __init__.py       # Bridge protocol, load_bridges()
│           └── registry.py
├── tests/
│   ├── conftest.py
│   ├── test_boundaries.py
│   ├── core/
│   └── sentinel/
├── deploy/
│   ├── README.md
│   ├── systemd/friday-sentinel.service
│   ├── launchd/com.friday.sentinel.plist
│   └── setup_remote.sh           # ← setup_remote.sh
└── data/                         # git-ignored; created on first run
```

### Migration map (all via `git mv`)

| From | To |
|---|---|
| `friday_hub.py` | `friday/desktop/hub.py` |
| `app_desktop.py` | `friday/desktop/app.py` |
| `friday_agents.py` | `friday/desktop/agents.py` |
| `widget_generator_agent.py` | `friday/desktop/widget_generator.py` |
| `services/asset_generator.py` | `friday/desktop/asset_generator.py` |
| `sentry_*.py` (7 files) | `friday/desktop/sentry_*.py` |
| `web_gui/` | `friday/desktop/web_gui/` |
| `face_detection_yunet.onnx`, `face_recognition_sface.onnx` | `friday/desktop/models/` |
| `setup_remote.sh` | `deploy/setup_remote.sh` |
| `friday_memory.json`, `friday_profiles.json`, `friday_scenes.json`, `friday_assets.json`, `friday_history.jsonl`, `generated_assets/`, `.webview/` | `data/` (plain `mv`; all git-ignored) |

### Deleted

`requirements.txt`, `test_conn.py`, `test_conn_live.py`, `friday_plugins/`
(contains only `__pycache__`), `services/`, `friday_visualization.html`
(unreferenced), root `__pycache__/`.

## 4. Packaging

`pyproject.toml` (setuptools backend):

- `[project]` name `friday`, `requires-python = ">=3.11"`, version from
  `friday/__init__.py`.
- Base dependencies (shared by every node): `python-dotenv`, `psutil`,
  `aiohttp`, `google-genai`.
- `[project.optional-dependencies]`
  - `desktop`: `pyaudio`, `opencv-python`, `pyautogui`, `pillow`, `numpy`,
    `pyobjc-core`, `pyobjc-framework-Quartz`, `pyobjc-framework-EventKit`,
    `pywebview`, `websockets`, `httpx`.
  - `sentinel`: *(empty — base deps suffice; exists so the install command
    reads clearly and has somewhere to grow)*.
  - `dev`: `pytest`, `pytest-asyncio`, `pytest-aiohttp`.
- `[project.scripts]` `friday-sentinel = "friday.sentinel.__main__:main"`,
  `friday-desktop = "friday.desktop.__main__:main"`.
- `[tool.pytest.ini_options]` `asyncio_mode = "auto"`, `testpaths = ["tests"]`.
- Package data: `friday/desktop/web_gui/**` and `friday/desktop/models/*`.

Install: Mac `pip install -e ".[desktop,dev]"`; server
`pip install -e ".[sentinel]"`.

## 5. `friday.core`

### 5.1 `config.py`

```python
@dataclass(frozen=True)
class Settings:
    repo_root: Path
    data_dir: Path
    node_id: str
    sentinel_bind_host: str
    sentinel_bind_port: int
    sentinel_url: str | None
    sentinel_token: str | None
    db_synchronous: Literal["FULL", "NORMAL"]
    telemetry_interval_s: float
    heartbeat_interval_s: float
    retention_days: int
    log_level: str
    bridges: tuple[str, ...]
    llm_routes: Mapping[str, str]      # role → "provider:model"
    gemini_api_key: str | None
    gemini_model: str                  # legacy alias feeding the "live" role
    friday_voice: str
    tripo_api_key: str | None

def load_settings(env: Mapping[str, str] | None = None,
                  env_file: Path | None = None) -> Settings
def get_settings() -> Settings          # load_settings() once per process, cached
```

- `env` defaults to `os.environ`; tests pass a dict.
- `.env` lookup: explicit `env_file`, else `<repo_root>/.env`. Values from
  `.env` never override an already-set process variable (dotenv semantics).
- `repo_root = Path(__file__).resolve().parents[2]`.
- Invalid values (non-int port, unknown sync mode) raise `ConfigError` with the
  variable name in the message.
- `data_dir` is created on first `load_settings()` call (`mkdir -p`).
- `get_settings()` is the process-wide accessor for modules that can't be
  handed a `Settings` (e.g. `agents.py` reading its routed model at import);
  tests call `load_settings(env={...})` directly and never touch the cache.

| Variable | Default | Notes |
|---|---|---|
| `FRIDAY_DATA_DIR` | `<repo>/data` | |
| `FRIDAY_NODE_ID` | `platform.node()` | |
| `FRIDAY_SENTINEL_BIND` | `127.0.0.1:8770` | `host:port` |
| `FRIDAY_SENTINEL_URL` | unset | e.g. `https://server.tailnet.ts.net/sentinel` |
| `FRIDAY_SENTINEL_TOKEN` | unset | if set, `POST /events` and `/ws` require it |
| `FRIDAY_DB_SYNCHRONOUS` | `FULL` | `FULL` or `NORMAL` |
| `FRIDAY_TELEMETRY_INTERVAL` | `15` | seconds |
| `FRIDAY_HEARTBEAT_INTERVAL` | `30` | seconds |
| `FRIDAY_RETENTION_DAYS` | `14` | telemetry rows and done/failed events older than this are pruned |
| `FRIDAY_LOG_LEVEL` | `INFO` | |
| `FRIDAY_BRIDGES` | empty | comma-separated dotted class paths |
| `FRIDAY_LLM_LIVE` | `gemini:gemini-3.1-flash-live-preview` | `GEMINI_MODEL`, if set, overrides this default (backward compatibility) |
| `FRIDAY_LLM_AGENT_OS` | `gemini:gemini-3.8-flash` | |
| `FRIDAY_LLM_AGENT_SPATIAL` | `gemini:gemini-3.8-flash` | |
| `FRIDAY_LLM_WIDGET` | `gemini:gemini-3.7-flash` | |
| `FRIDAY_LLM_TRIAGE` | `gemini:gemini-3.7-flash` | reserved; unused this pass |
| `GEMINI_API_KEY` | unset | required by desktop; optional for sentinel |
| `GEMINI_MODEL` | unset | legacy alias, see above |
| `FRIDAY_VOICE` | `Aoede` | |
| `TRIPO_API_KEY` | unset | |

### 5.2 `platform.py`

```python
@dataclass(frozen=True)
class PlatformInfo:
    system: str          # "Darwin" | "Linux" | other
    machine: str         # "arm64" | "x86_64" | "aarch64" | ...
    arch_family: str     # "arm64" | "x86_64" | "other"  (normalised)
    hostname: str
    python: str
    is_raspberry_pi: bool
    distro: str | None   # from /etc/os-release PRETTY_NAME; None on macOS

def detect(proc_root: Path = Path("/"), ...) -> PlatformInfo
```

- `is_raspberry_pi`: `/proc/device-tree/model` exists and contains
  "Raspberry Pi".
- `arch_family` maps `aarch64`/`arm64` → `arm64`, `x86_64`/`AMD64` → `x86_64`.
- Root path injectable for tests. Never raises.

### 5.3 `storage.py`

`Store` is synchronous, single-connection, stdlib `sqlite3`.

```python
class Store:
    @classmethod
    def open(cls, path: Path, *, synchronous: str = "FULL") -> "Store"
    def close(self) -> None
    def checkpoint(self, mode: str = "TRUNCATE") -> None

    # kv
    def kv_get(self, key: str) -> Any | None
    def kv_set(self, key: str, value: Any) -> None       # JSON-encoded
    def kv_delete(self, key: str) -> None

    # heartbeats
    def heartbeat_upsert(self, node_id: str, status: str, meta: dict, ts: float) -> None
    def heartbeats(self) -> list[HeartbeatRow]

    # event queue
    def enqueue(self, event: Event) -> None
    def claim(self, limit: int, now: float) -> list[Event]
    def complete(self, event_id: str, now: float) -> None
    def fail(self, event_id: str, error: str, now: float, *, retry: bool) -> None
    def requeue_stale(self, older_than_s: float, now: float) -> int
    def queue_depths(self) -> dict[str, int]              # status → count

    # telemetry
    def telemetry_insert(self, node_id: str, snapshot: dict, ts: float) -> None
    def telemetry_latest(self, node_id: str | None = None) -> dict | None
    def prune(self, older_than_ts: float) -> dict[str, int]
```

**Open sequence**

1. `mkdir -p` parent.
2. Refuse to start if `sqlite3.sqlite_version_info < (3, 35)` (`RETURNING`
   support) with a `ConfigError` naming the version found.
3. Connect with `isolation_level=None` (autocommit; explicit `BEGIN`/`COMMIT`
   where multi-statement atomicity matters). sqlite3's default same-thread
   check is left **on**: `AsyncStore.open()` runs `Store.open` on its pool
   thread, so every call — including open — happens on one thread and the
   check guards that invariant for free.
4. Pragmas, in order: `journal_mode=WAL`, `synchronous=<FULL|NORMAL>`,
   `busy_timeout=5000`, `journal_size_limit=67108864`, `foreign_keys=ON`,
   `temp_store=MEMORY`.
5. `PRAGMA quick_check`. On anything but `ok`: close, rename the db file and
   its `-wal`/`-shm` siblings to `<name>.corrupt-<unix-ts>`, log at ERROR,
   reopen fresh.
6. Migrate: `schema_version` table; apply `MIGRATIONS[n]` for `n >
   current`, each in one transaction, bumping the version inside it.

**Schema v1**

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE kv (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE heartbeats (
  node_id TEXT PRIMARY KEY, last_seen REAL NOT NULL,
  status TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,
  type TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|processing|done|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, claimed_at REAL, processed_at REAL, error TEXT);
CREATE INDEX events_status_ts ON events(status, priority DESC, ts);
CREATE TABLE telemetry (
  ts REAL NOT NULL, node_id TEXT NOT NULL, snapshot TEXT NOT NULL);
CREATE INDEX telemetry_node_ts ON telemetry(node_id, ts DESC);
```

**Queue semantics**

- `claim(limit)`:
  `UPDATE events SET status='processing', claimed_at=?, attempts=attempts+1
   WHERE id IN (SELECT id FROM events WHERE status='pending'
   ORDER BY priority DESC, ts LIMIT ?) RETURNING *` — atomic, single
  statement. Requires SQLite ≥ 3.35 (Pi OS Bookworm ships 3.40).
- `fail(..., retry=True)` sets `status='pending'`, `error=?`; `retry=False`
  sets `status='failed'`. The bus decides `retry` from `attempts <
  max_attempts`.
- `requeue_stale(older_than_s)` returns rows `processing` with
  `claimed_at < now - older_than_s` to `pending`. Called once at boot with
  `older_than_s=0` (anything processing at boot was orphaned by a crash).
- `prune(older_than_ts)` deletes `telemetry` rows and `done`/`failed` events
  older than the cutoff. `pending`/`processing` are never pruned.

**`AsyncStore`** (same module): wraps a `Store` and a
`ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-store")`.
Every method is `async def name(...) -> await loop.run_in_executor(self._pool,
partial(self._store.name, ...))`. `aclose()` checkpoints, closes the store and
shuts the pool down.

### 5.4 `events.py`

```python
EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

@dataclass(frozen=True)
class Event:
    type: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    priority: int = 0

    def validate(self) -> None                   # raises EventValidationError
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: Mapping) -> "Event"    # builds, then validate()

@dataclass(frozen=True)
class Heartbeat:               # payload schema for node.heartbeat
    status: str                # free text: "listening", "idle", "booting", ...
    version: str
    platform: str              # PlatformInfo.system/arch summary
    meta: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TelemetrySnapshot:       # payload schema for telemetry.sample
    ts: float
    node_id: str
    platform: PlatformInfo
    cpu_percent: float | None
    load_avg: tuple[float, float, float] | None
    mem_total: int | None
    mem_used: int | None
    mem_available: int | None
    disk_total: int | None
    disk_used: int | None
    disk_free: int | None
    disk_path: str
    uptime_s: float | None
    thermal: Mapping[str, float]          # sensor label → °C; empty when none
    power: PowerInfo | None

@dataclass(frozen=True)
class PowerInfo:
    battery_percent: float | None
    on_ac: bool | None
    throttled_flags: int | None           # raw vcgencmd bitmask, Pi only
    under_voltage: bool | None
```

`Event.validate()`: `type` matches `EVENT_TYPE_RE`, `source` non-empty,
`payload` is a mapping and JSON-encodable, `ts` numeric, `priority` int.
`from_dict` builds then validates; `EventBus.publish` validates again on
the way in. `Heartbeat` has `to_dict/from_dict`; `TelemetrySnapshot` and
`PowerInfo` are write-only (`to_dict` via `dataclasses.asdict`).

Reserved event types this pass: `node.heartbeat`, `telemetry.sample`,
`sentinel.started`, `sentinel.stopping`. Future (documented, not handled):
`email.received`, `sensor.update`, `message.received`, `command.*`.

### 5.5 `telemetry.py`

```python
def collect(node_id: str, data_dir: Path, *, platform: PlatformInfo | None = None,
            sys_root: Path = Path("/")) -> TelemetrySnapshot
```

Each probe is its own function returning `None` (or `{}`) on any exception,
logging at DEBUG the first time it fails:

| Probe | Source | Absent on |
|---|---|---|
| `cpu_percent` | `psutil.cpu_percent(interval=None)` (primed once at import of the monitor) | — |
| `load_avg` | `os.getloadavg()` | Windows (n/a) |
| memory | `psutil.virtual_memory()` | — |
| disk | `psutil.disk_usage(data_dir)` | — |
| uptime | `time.time() - psutil.boot_time()` | — |
| thermal | `psutil.sensors_temperatures()` → flatten to `label: current`; if empty, scan `<sys_root>/sys/class/thermal/thermal_zone*/{type,temp}` (milli-°C) | macOS (both empty → `{}`) |
| power.battery | `psutil.sensors_battery()` | desktops, Pi |
| power.throttled | `vcgencmd get_throttled` via `shutil.which("vcgencmd")`, parse `throttled=0x...`; `under_voltage = bool(flags & 0x1)` | everything but Pi |

### 5.6 `logging.py`

`configure_logging(level: str) -> None`. Single stdout `StreamHandler`.
Format `%(asctime)s %(levelname)-5s %(name)s: %(message)s`; when
`JOURNAL_STREAM` is in the environment (systemd is capturing stdout) the
timestamp is dropped because journald stamps every line. Idempotent.

### 5.7 `llm/`

```python
# routing.py
@dataclass(frozen=True)
class Route:
    provider: str      # "gemini"
    model: str

ROLES = ("live", "agent_os", "agent_spatial", "widget", "triage")
def parse_route(spec: str) -> Route            # "gemini:gemini-3.7-flash"; ConfigError on bad format
def resolve(settings: Settings, role: str) -> Route

# base.py
@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, Any]

@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any                                    # provider-native response

class LLMProvider(Protocol):
    name: str
    model: str
    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse

# gemini.py
class GeminiProvider:                            # implements LLMProvider
    def __init__(self, client: genai.Client, model: str)
    # tools: list of JSON-schema function declarations; mapped to
    # types.FunctionDeclaration. automatic_function_calling disabled.

# __init__.py
def gemini_client(settings: Settings) -> genai.Client   # one per process, cached
def get_provider(settings: Settings, role: str) -> LLMProvider
```

Unknown provider names raise `ConfigError` naming the variable. Desktop
callers use `resolve(...).model` and `gemini_client(...)` only; they do not
go through `generate()` this pass.

## 6. `friday.sentinel`

### 6.1 `daemon.py`

```python
class Sentinel:
    def __init__(self, settings: Settings)
    async def run(self) -> int          # exit code
    def request_shutdown(self, reason: str) -> None   # thread-safe
```

Boot order in `run()`:

1. `configure_logging`; log `PlatformInfo` and `data_dir`.
2. Install `SIGINT`/`SIGTERM` → `request_shutdown(signame)`;
   `SIGHUP` → log "reload not supported; ignoring". Wrapped in `try/except
   NotImplementedError` (Windows) so a missing signal never aborts boot.
   Installed first so a signal during boot still produces an orderly exit.
3. `AsyncStore.open(data_dir / "sentinel.db")`; `requeue_stale(0)`; log
   counts.
4. `EventBus(store)`; subscribe `HeartbeatHandler`, `TelemetryHandler`,
   `LogHandler`.
5. `load_bridges(settings)` → `await bridge.start(bus)` for each, in order;
   a bridge whose `start` raises is logged at ERROR and skipped.
6. `api.start(settings, bus, store, health_ctx)`.
7. Monitors under `_supervise`: `TelemetryMonitor`, `SelfHeartbeat`,
   `Housekeeping`, `bus.run_dispatcher`.
8. `bus.publish(Event("sentinel.started", ...))`; `sd_notify("READY=1")`.
9. `await shutdown_event.wait()`. If shutdown was requested during boot,
   whatever has been started so far is torn down in the same order below.

Shutdown (`asyncio.wait_for(..., SHUTDOWN_TIMEOUT_S=10)`; on timeout, cancel
what's left and continue):

1. `sd_notify("STOPPING=1")`; publish `sentinel.stopping`.
2. `api.stop()` — stop accepting; close WS clients with code 1001.
3. Cancel monitors; `await bridge.stop()` for each (each with a 3 s cap).
4. `bus.drain()` — dispatcher finishes in-flight handlers, then exits.
5. `store.aclose()` — `checkpoint("TRUNCATE")` then close.
6. Return 0.

`_supervise(name, coro_factory)`: runs the coroutine; on an exception that
isn't `CancelledError`, logs with traceback, sleeps `min(30, 2**n)` and
restarts, unless shutdown is in progress. Restart count is exposed in
`/health`.

`sd_notify(state: str)`: if `NOTIFY_SOCKET` is unset, return. Else send the
datagram over `AF_UNIX`/`SOCK_DGRAM` (abstract namespace if the path starts
with `@`). Any error is swallowed and logged once.

`__main__.py`: `main()` → `load_settings()` → `asyncio.run(Sentinel(s).run())`
→ `sys.exit(code)`. A `ConfigError` or store-open failure prints one line to
stderr and exits 1.

### 6.2 `bus.py`

```python
class EventBus:
    def __init__(self, store: AsyncStore, *, max_attempts: int = 3,
                 handler_timeout_s: float = 30.0, batch: int = 16)
    def subscribe(self, handler: Handler) -> None
    def unsubscribe(self, handler: Handler) -> None
    async def publish(self, event: Event) -> str            # returns event.id
    async def run_dispatcher(self) -> None                   # long-running
    async def drain(self) -> None
    def handlers_for(self, event_type: str) -> list[Handler]
```

- `publish`: `event.validate()`, `await store.enqueue(event)`,
  `self._wake.set()`. Returns after the row is
  durable.
- Dispatcher loop: `await wake or timeout(1s)`; `claim(batch)`; for each
  event, for each matching handler:
  `await asyncio.wait_for(handler.handle(event, ctx), handler_timeout_s)`.
  All handlers succeed → `complete`. Any raises → `fail(retry = attempts <
  max_attempts)`, error text = `f"{handler.name}: {type(e).__name__}: {e}"`,
  remaining handlers for that event still run (one bad handler doesn't
  starve the others; the retry re-runs all of them — idempotency required).
- `HandlerContext` carries `store`, `bus`, `settings`, `logger`.
- Live fan-out to WebSocket subscribers is implemented as a handler
  (`WebSocketFanout` in `api.py`), not a special path in the bus.

### 6.3 `handlers.py`

```python
class Handler(Protocol):
    name: str
    patterns: Sequence[str]                     # fnmatch on event.type
    async def handle(self, event: Event, ctx: HandlerContext) -> None

class HeartbeatHandler:     patterns = ("node.heartbeat",)   # upsert heartbeats
class TelemetryHandler:     patterns = ("telemetry.sample",) # insert telemetry
class LogHandler:           patterns = ("*",)                # INFO line: type source id
```

`LogHandler` is documented as the triage seam: a future `TriageHandler`
subscribes to `*`, calls `get_provider(settings, "triage").generate(...)` and
publishes `command.*` events for bridges.

### 6.4 `api.py`

aiohttp application bound to `FRIDAY_SENTINEL_BIND`.

| Route | Auth | Response |
|---|---|---|
| `GET /health` | none | `200 {"status":"ok","node_id","version","uptime_s","platform":{...},"queue":{"pending","processing","done","failed"},"supervisor_restarts":{...}}` |
| `GET /telemetry` | none | `200` latest `TelemetrySnapshot` dict for this node, or `204` |
| `GET /nodes` | none | `200 [{"node_id","last_seen","status","meta"}]` |
| `POST /events` | bearer if token set | body: one event dict or a list (≤ 100). `202 {"ids":[...]}`. `400` with `{"error"}` on validation failure (whole batch rejected). `401` bad/missing token. `413` over 256 KB. |
| `GET /ws` | bearer if token set (header, or `?token=` for browsers) | WebSocket. First client message: `{"subscribe":["node.*","telemetry.sample"]}` (default `["*"]`). Server then sends each matching bus event as its dict. Ping every 20 s. |

- `Authorization: Bearer <token>` compared with `hmac.compare_digest`.
- `source` in a posted event is taken as given (nodes name themselves);
  `ts` defaults to server time if absent.
- `WebSocketFanout` handler (`patterns=("*",)`) pushes to every connected
  socket whose subscription matches; a slow socket (send > 2 s) is closed.
  It never raises — a push failure closes that one socket and nothing
  else — so a dead browser can never force a bus retry.
- Server is started with `aiohttp.web.AppRunner` + `TCPSite`; `stop()`
  awaits `runner.cleanup()`.

### 6.5 `monitors.py`

Each is `async def run(ctx)` and is meant to be wrapped in `_supervise`:

- `TelemetryMonitor(interval_s)`: `collect(...)` in the default executor
  (psutil calls can block briefly) → `bus.publish(Event("telemetry.sample",
  payload=snapshot.to_dict()))`.
- `SelfHeartbeat(interval_s)`: publish `node.heartbeat` with
  `Heartbeat(status="running", ...)`; `sd_notify("WATCHDOG=1")`.
- `Housekeeping(interval_s=3600)`: `store.prune(now - retention_days*86400)`;
  `store.checkpoint("PASSIVE")`; log counts.

### 6.6 `bridges/`

```python
class Bridge(Protocol):
    name: str
    async def start(self, bus: EventBus, ctx: HandlerContext) -> None
    async def stop(self) -> None

def load_bridges(settings: Settings) -> list[Bridge]
```

`load_bridges` imports each dotted path in `settings.bridges`
(`importlib.import_module` + `getattr`), instantiates with no arguments, and
returns the list. A path that fails to import raises `ConfigError` at boot
(misconfiguration is fatal; runtime failure inside a bridge is not).

A bridge publishes inbound events and subscribes handlers for the
`command.*` events it can act on. Nothing in the sentinel knows what a
bridge does.

### 6.7 `deploy/`

`systemd/friday-sentinel.service` (documented placeholders `__USER__`,
`__REPO__`):

```ini
[Unit]
Description=FRIDAY sentinel
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=__USER__
WorkingDirectory=__REPO__
EnvironmentFile=__REPO__/.env
ExecStart=__REPO__/.venv/bin/python -m friday.sentinel
Restart=always
RestartSec=5
WatchdogSec=90
TimeoutStopSec=20
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=__REPO__/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`WatchdogSec` (90 s) is deliberately three times the heartbeat interval
(30 s) so one missed beat never triggers a restart.

`launchd/com.friday.sentinel.plist`: `Label`, `ProgramArguments`
(`__REPO__/.venv/bin/python -m friday.sentinel`), `WorkingDirectory`,
`KeepAlive` true, `RunAtLoad` true, `StandardOutPath`/`StandardErrorPath`
under `__REPO__/data/logs/`.

`deploy/README.md`: substitution, `systemctl enable --now`, `journalctl -u
friday-sentinel -f`, `launchctl bootstrap gui/$(id -u) …`, Tailscale Serve
line for port 8770, `curl /health` smoke check.

## 7. Desktop integration

Changes to `hub.py` and siblings, and nothing else:

1. Imports: `from friday.desktop import sentry_vision, sentry_exec, …,
   agents, widget_generator, asset_generator`. Module attribute access at
   call sites is unchanged.
2. `settings = load_settings()` at module import (replaces `load_dotenv()`).
3. Paths: `HISTORY_LOG_FILE`, `MEMORY_FILE`, `sentry_scene.SCENES_FILE`,
   recognition `PROFILES_FILE`, asset index, `generated_assets/`, `.webview`
   → under `settings.data_dir`. `web_gui/` → `Path(__file__).parent /
   "web_gui"`; ONNX models → `Path(__file__).parent / "models"`.
4. Models: `MODEL_ID = resolve(settings, "live").model`;
   `agents.OS_AGENT_MODEL`/`SVE_AGENT_MODEL` and
   `widget_generator.WIDGET_MODEL` read their role at import.
5. Client: `genai.Client(api_key=...)` → `gemini_client(settings)`.
6. `sentinel_client.py`:

   ```python
   class SentinelClient:
       def __init__(self, url: str, token: str | None, node_id: str)
       async def post(self, event: Event) -> bool     # never raises; False on any failure
       async def aclose(self) -> None
   ```

   Hub: if `settings.sentinel_url`, start `heartbeat_task()` posting
   `node.heartbeat` every `heartbeat_interval_s` with
   `Heartbeat(status=<current system status>, version, platform)`. Failures
   are logged at WARNING once, then at DEBUG until the next success.
7. `app.py` keeps its lifecycle; `__main__.py` calls `app.main()`.

## 8. Cross-platform guarantees

- `tests/test_boundaries.py`: for each of `friday.core`, `friday.sentinel`
  (and every submodule, walked with `pkgutil`), import in a fresh
  interpreter (`subprocess`) with a `sitecustomize` that installs a
  `sys.meta_path` finder raising `ImportError` for `Quartz`, `pyaudio`,
  `cv2`, `webview`, `EventKit`, `Foundation`, `AppKit`, `objc`,
  `pyautogui`, `termios`, `tty`, `PIL`, `numpy`. Any leak fails. (`httpx`
  and `websockets` are *not* poisoned: `google-genai`, a base dependency,
  requires both.)
- No literal absolute paths in Python outside `platform.py`/`telemetry.py`
  probes, and those take an injectable root.
- No `platform.system() == "Darwin"` branches in `core`/`sentinel`; feature
  probing only (does the file/binary/socket exist).
- `deploy/` files are the only OS-specific artefacts and are templates.

## 9. Error handling

| Situation | Behaviour |
|---|---|
| `ConfigError` at boot | one line to stderr, exit 1 |
| Store cannot be opened (permissions, disk full) | exit 1; systemd restarts after `RestartSec` |
| `quick_check` fails | move aside, recreate, ERROR log; continue |
| Handler raises / times out | `fail(retry=attempts<3)`; after 3, `failed` with error text; dispatcher continues |
| Monitor/dispatcher task raises | `_supervise` logs, backs off, restarts; counted in `/health` |
| Bridge `start` raises | logged, bridge skipped; daemon continues |
| API invalid body / auth / size | 400 / 401 / 413 JSON bodies |
| Telemetry probe raises | that field is `None`/`{}`; DEBUG log once |
| Shutdown exceeds 10 s | remaining tasks cancelled; store still closed |
| Desktop cannot reach sentinel | `SentinelClient.post` returns False; voice path unaffected |

## 10. Testing

pytest, `asyncio_mode=auto`. Every test uses `tmp_path` for `data_dir`.

**core**
- `test_config.py`: defaults, overrides, `.env` precedence, bad port/sync
  mode raise `ConfigError`, `GEMINI_MODEL` alias, route parsing.
- `test_platform.py`: Pi detection via fake `/proc/device-tree/model`,
  `arch_family` normalisation, missing `/etc/os-release` → `None`.
- `test_storage.py`: pragmas asserted after open (`journal_mode == "wal"`,
  `synchronous` matches), migration from empty, kv round-trip, heartbeat
  upsert, `enqueue/claim/complete`, `claim` ordering by priority then ts,
  `fail` retry vs terminal, `requeue_stale`, `prune` never touches
  pending/processing, corrupt file moved aside (write garbage, open,
  assert `.corrupt-*` exists and store works), `AsyncStore` runs on one
  thread (assert `threading.get_ident()` inside two calls is equal).
- `test_events.py`: validation matrix, `to_dict/from_dict` round-trip,
  non-JSON payload rejected at `enqueue`.
- `test_telemetry.py`: `collect` returns a snapshot on this host; with every
  psutil function monkeypatched to raise, still returns with `None` fields;
  sysfs thermal parsed from a fake tree; `vcgencmd` parsed from a fake
  binary on `PATH`; absent binary → `None`.
- `test_llm_routing.py`: role table, bad spec, unknown provider; provider
  construction does not touch the network.

**sentinel**
- `test_bus.py`: publish → handler receives; pattern matching; handler
  failure → retried then `failed`; one failing handler doesn't block another;
  timeout enforced; `drain` finishes in-flight.
- `test_api.py` (`aiohttp` test client): `/health` shape; `/events` 202 /
  400 / 401 / 413; list batch; `/nodes` reflects a posted heartbeat; `/ws`
  receives a published event matching its subscription and not one that
  doesn't.
- `test_monitors.py`: each monitor publishes the expected event type once
  per tick (interval patched to ~0).
- `test_daemon.py`: `Sentinel.run()` in a task; wait for `/health`; send
  `SIGTERM` to own pid; assert exit 0 within 10 s, `-wal` file is empty or
  gone, `sentinel.stopping` was enqueued. Second test: `requeue_stale`
  path — pre-seed a `processing` row, boot, assert it's `pending`.
- `test_bridges.py`: `load_bridges` with a test double: `start` called with
  the bus, `stop` called on shutdown, bad path → `ConfigError`.

**boundaries**: as in §8.

**desktop**: `tests/desktop/test_imports.py` — `import friday.desktop.hub`
succeeds *only if* the desktop extra is installed (skip otherwise). Runs on
the Mac.

## 11. Implementation phases

1. **Skeleton** — `pyproject.toml`, `friday/__init__.py`, empty `core`,
   `desktop`, `sentinel` packages, `tests/conftest.py`. `pytest` green.
2. **core** — in order: `config` → `platform` → `events` → `storage` →
   `telemetry` → `logging` → `llm`. TDD per module.
3. **sentinel** — `handlers` → `bus` → `api` → `monitors` → `bridges` →
   `daemon`. `python -m friday.sentinel` runs on the Mac; `curl
   127.0.0.1:8770/health` answers.
4. **deploy** — unit, plist, README.
5. **desktop relocation** — `git mv` per the map; import fixes; `data_dir`;
   routed models; shared client; `sentinel_client` + heartbeat task;
   `__main__.py`. `python -m friday.desktop` boots on the Mac.
6. **cleanup + docs** — deletions; `.gitignore` (`data/`, drop the
   per-file entries); `.env.template` (new variables, grouped); `readme.md`
   rewritten for the new layout, run commands, sentinel API and deploy
   story, verified against source.
7. **verification** — full suite; boundary test; sentinel foreground run +
   `curl`; desktop boot; leave everything uncommitted.

## 12. Out of scope, recorded for later

- `TriageHandler` and its policy.
- OpenAI-compatible and Anthropic providers.
- Migrating desktop agents onto `LLMProvider.generate()`.
- Splitting `hub.py` into audio / gateway / tools / session modules.
- Renaming `sentry_*` modules.
- Concrete bridges: Termux telephony, WhatsApp, cloud voice, MQTT.
- CI matrix.
