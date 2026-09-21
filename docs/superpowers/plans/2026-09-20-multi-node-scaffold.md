# FRIDAY Multi-Node Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure FRIDAY into a `friday/` package with `core`, `desktop` and `sentinel` sub-packages, and build the cross-platform sentinel daemon (SQLite/WAL event queue, telemetry, HTTP+WS API, bridge protocol) so the same code runs on macOS, Fedora x86_64 and Raspberry Pi OS ARM64.

**Architecture:** `friday.core` holds everything shared (settings, SQLite store, event schemas, telemetry probes, LLM routing). `friday.sentinel` is an asyncio daemon: a durable event bus over the store, an aiohttp API nodes push events into, monitors that sample telemetry, and a `Bridge` protocol for future telephony/messaging adapters. `friday.desktop` is the existing macOS app relocated intact, reading its paths and model ids from `core` and posting a heartbeat to the sentinel.

**Tech Stack:** Python ≥ 3.11, stdlib `sqlite3` (WAL), `aiohttp`, `psutil`, `python-dotenv`, `google-genai`; `pytest` + `pytest-asyncio` + `pytest-aiohttp`.

**Spec:** `docs/superpowers/specs/2026-09-20-multi-node-scaffold-design.md`

## Global Constraints

- **No commits.** The user's global rule: never run `git commit`. Every task ends with the work left in the working tree. `git mv` / `git rm` (index-only staging) are fine.
- Python floor `>=3.11`; no 3.12+ syntax (no `type X = ...`, no PEP 695 generics).
- `friday.core` and `friday.sentinel` import only stdlib + `psutil`, `aiohttp`, `python-dotenv`, `google-genai`. Never `Quartz`, `pyaudio`, `cv2`, `webview`, `EventKit`, `Foundation`, `AppKit`, `objc`, `pyautogui`, `termios`, `tty`, `PIL`, `numpy`. Task 17 enforces this.
- All runtime state lives under `Settings.data_dir` (`FRIDAY_DATA_DIR`, default `<repo>/data`). No literal absolute paths in Python outside injectable probe roots.
- No `platform.system() == "Darwin"` branches in `core`/`sentinel`; probe for files/binaries/sockets instead.
- Use the venv interpreter for everything: `.venv/bin/python`, `.venv/bin/pytest`.
- Every function/class named in a task's **Interfaces → Produces** block is a contract later tasks rely on; keep the exact names and signatures.
- Two deliberate deviations from the spec's file listing, both cosmetic: the logging module is `friday/core/logsetup.py` (a module literally named `logging.py` shadows the stdlib when run from its own directory), and `sd_notify` lives in `friday/sentinel/sdnotify.py` (importable by monitors without pulling in the daemon). `bridges/registry.py` is folded into `bridges/__init__.py`.
- All tests use `tmp_path`; nothing touches the real `<repo>/data` or the user's `.env` except `get_settings()`-cache tests, which clear the cache afterwards.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `pyproject.toml` | one distribution, extras `desktop` / `sentinel` / `dev`, pytest config |
| `friday/__init__.py` | `__version__` |
| `friday/core/config.py` | `Settings`, `load_settings`, `get_settings`, `ConfigError` |
| `friday/core/platform.py` | `PlatformInfo`, `detect`, `normalise_arch` |
| `friday/core/events.py` | `Event`, `Heartbeat`, `TelemetrySnapshot`, `PowerInfo`, `EventValidationError` |
| `friday/core/storage.py` | `Store` (sync SQLite/WAL), `AsyncStore`, `StoredEvent`, `HeartbeatRow` |
| `friday/core/telemetry.py` | `collect()` and its tolerant probes |
| `friday/core/logsetup.py` | `configure_logging` |
| `friday/core/llm/{__init__,base,routing,gemini}.py` | routing, provider protocol, Gemini adapter |
| `friday/sentinel/handlers.py` | `Handler`, `HandlerContext`, `matches`, built-in handlers |
| `friday/sentinel/bus.py` | `EventBus` |
| `friday/sentinel/sdnotify.py` | `sd_notify` |
| `friday/sentinel/api.py` | `create_app`, `ApiServer`, `HealthState`, `WebSocketFanout` |
| `friday/sentinel/monitors.py` | `TelemetryMonitor`, `SelfHeartbeat`, `Housekeeping` |
| `friday/sentinel/bridges/__init__.py` | `Bridge`, `load_bridges` |
| `friday/sentinel/daemon.py`, `friday/sentinel/__main__.py` | `Sentinel`, `main` |
| `friday/desktop/sentinel_client.py`, `friday/desktop/__main__.py` | heartbeat poster, entrypoint |
| `tests/**` | see tasks |
| `deploy/**` | systemd unit, launchd plist, README |

**Moved** (Task 19): every existing Python module and `web_gui/` into `friday/desktop/`, the two `.onnx` files into `friday/desktop/models/`, `setup_remote.sh` into `deploy/`, runtime state files into `data/`.

**Deleted** (Task 21): `requirements.txt`, `test_conn.py`, `test_conn_live.py`, `friday_visualization.html`, `friday_plugins/`, `services/`.

---

### Task 1: Packaging skeleton

**Files:**
- Create: `pyproject.toml`, `friday/__init__.py`, `friday/core/__init__.py`, `friday/core/llm/__init__.py`, `friday/desktop/__init__.py`, `friday/sentinel/__init__.py`, `friday/sentinel/bridges/__init__.py`, `tests/__init__.py`, `tests/core/__init__.py`, `tests/sentinel/__init__.py`, `tests/desktop/__init__.py`, `tests/conftest.py`, `tests/test_version.py`

**Interfaces:**
- Produces: `friday.__version__: str`; importable packages `friday.core`, `friday.core.llm`, `friday.desktop`, `friday.sentinel`, `friday.sentinel.bridges`; `tests.*` importable as packages.

- [ ] **Step 1: Write the failing test**

`tests/test_version.py`:
```python
import re


def test_version_is_semver():
    import friday

    assert re.fullmatch(r"\d+\.\d+\.\d+", friday.__version__)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_version.py -v`
Expected: FAIL / error — `friday` not importable (or pytest not installed yet).

- [ ] **Step 3: Create the package tree and pyproject**

```bash
mkdir -p friday/core/llm friday/desktop friday/sentinel/bridges tests/core tests/sentinel tests/desktop
printf '__version__ = "0.1.0"\n' > friday/__init__.py
for d in friday/core friday/core/llm friday/desktop friday/sentinel friday/sentinel/bridges tests tests/core tests/sentinel tests/desktop; do : > "$d/__init__.py"; done
```

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "friday"
dynamic = ["version"]
description = "FRIDAY - a multi-node personal assistant: macOS desktop node + headless sentinel daemon"
requires-python = ">=3.11"
dependencies = [
  "python-dotenv",
  "psutil",
  "aiohttp",
  "google-genai",
]

[project.optional-dependencies]
# The macOS voice/HUD node. Everything here is platform- or hardware-bound.
desktop = [
  "pyaudio",
  "opencv-python",
  "pyautogui",
  "pillow",
  "numpy",
  "pyobjc-core",
  "pyobjc-framework-Quartz",
  "pyobjc-framework-EventKit",
  "pywebview",
  "websockets",
  "httpx",
]
# The headless daemon needs nothing beyond the base dependencies. The extra
# exists so `pip install -e ".[sentinel]"` reads clearly and has room to grow.
sentinel = []
dev = [
  "pytest",
  "pytest-asyncio",
  "pytest-aiohttp",
]

[project.scripts]
friday-sentinel = "friday.sentinel.__main__:main"
friday-desktop = "friday.desktop.__main__:main"

[tool.setuptools.packages.find]
include = ["friday*"]

[tool.setuptools.dynamic]
version = {attr = "friday.__version__"}

[tool.setuptools.package-data]
"friday.desktop" = ["web_gui/**/*", "models/*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`tests/conftest.py`:
```python
"""Shared fixtures. Everything runs against tmp_path; nothing touches <repo>/data."""
```

- [ ] **Step 4: Install and verify**

Run: `.venv/bin/pip install -e ".[desktop,dev]" 2>&1 | tail -3`
Expected: `Successfully installed friday-0.1.0` (all other requirements already satisfied).

Run: `.venv/bin/pytest -v`
Expected: `tests/test_version.py::test_version_is_semver PASSED`

---

### Task 2: `friday.core.config`

**Files:**
- Create: `friday/core/config.py`
- Test: `tests/core/test_config.py`
- Modify: `tests/conftest.py` (add the `make_settings` fixture)

**Interfaces:**
- Produces:
  - `class ConfigError(ValueError)`
  - `REPO_ROOT: Path`, `LLM_ROLES: tuple[str, ...]`, `DEFAULT_LLM_ROUTES: Mapping[str, str]`
  - `@dataclass(frozen=True) class Settings` with fields exactly: `repo_root: Path, data_dir: Path, node_id: str, sentinel_bind_host: str, sentinel_bind_port: int, sentinel_url: str | None, sentinel_token: str | None, db_synchronous: str, telemetry_interval_s: float, heartbeat_interval_s: float, retention_days: int, log_level: str, bridges: tuple[str, ...], llm_routes: Mapping[str, str], gemini_api_key: str | None, gemini_model: str | None, friday_voice: str, tripo_api_key: str | None`
  - `load_settings(env: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings`
  - `get_settings() -> Settings` (lru_cache'd; has `.cache_clear()`)
  - conftest fixture `make_settings(**env) -> Settings` (data_dir under tmp_path, no `.env` read)

- [ ] **Step 1: Write the failing tests**

`tests/core/test_config.py`:
```python
import pytest

from friday.core.config import DEFAULT_LLM_ROUTES, ConfigError, Settings, load_settings


def test_defaults(make_settings, tmp_path):
    s = make_settings()
    assert isinstance(s, Settings)
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.data_dir.is_dir()
    assert (s.sentinel_bind_host, s.sentinel_bind_port) == ("127.0.0.1", 8770)
    assert s.sentinel_url is None and s.sentinel_token is None
    assert s.db_synchronous == "FULL"
    assert s.telemetry_interval_s == 15.0
    assert s.heartbeat_interval_s == 30.0
    assert s.retention_days == 14
    assert s.log_level == "INFO"
    assert s.bridges == ()
    assert dict(s.llm_routes) == dict(DEFAULT_LLM_ROUTES)
    assert s.gemini_api_key is None and s.gemini_model is None
    assert s.friday_voice == "Aoede"
    assert s.tripo_api_key is None
    assert s.node_id


def test_overrides(make_settings):
    s = make_settings(
        FRIDAY_NODE_ID="pi",
        FRIDAY_SENTINEL_BIND="0.0.0.0:9000",
        FRIDAY_SENTINEL_URL="https://x.ts.net/sentinel",
        FRIDAY_SENTINEL_TOKEN="t",
        FRIDAY_DB_SYNCHRONOUS="normal",
        FRIDAY_TELEMETRY_INTERVAL="5",
        FRIDAY_HEARTBEAT_INTERVAL="7.5",
        FRIDAY_RETENTION_DAYS="3",
        FRIDAY_LOG_LEVEL="debug",
        FRIDAY_BRIDGES="a.B, c.D,",
        FRIDAY_LLM_WIDGET="gemini:custom",
        FRIDAY_VOICE="Kore",
        GEMINI_API_KEY="k",
        TRIPO_API_KEY="tp",
    )
    assert s.node_id == "pi"
    assert (s.sentinel_bind_host, s.sentinel_bind_port) == ("0.0.0.0", 9000)
    assert s.sentinel_url == "https://x.ts.net/sentinel"
    assert s.sentinel_token == "t"
    assert s.db_synchronous == "NORMAL"
    assert s.telemetry_interval_s == 5.0
    assert s.heartbeat_interval_s == 7.5
    assert s.retention_days == 3
    assert s.log_level == "DEBUG"
    assert s.bridges == ("a.B", "c.D")
    assert s.llm_routes["widget"] == "gemini:custom"
    assert s.llm_routes["live"] == DEFAULT_LLM_ROUTES["live"]
    assert s.friday_voice == "Kore"
    assert s.gemini_api_key == "k"
    assert s.tripo_api_key == "tp"


def test_env_file_is_read_but_process_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FRIDAY_NODE_ID=from-file\nFRIDAY_LOG_LEVEL=WARNING\n")
    s = load_settings(
        env={"FRIDAY_DATA_DIR": str(tmp_path / "d"), "FRIDAY_NODE_ID": "from-env"},
        env_file=env_file,
    )
    assert s.node_id == "from-env"
    assert s.log_level == "WARNING"


def test_missing_env_file_is_fine(tmp_path):
    s = load_settings(env={"FRIDAY_DATA_DIR": str(tmp_path / "d")}, env_file=tmp_path / "nope.env")
    assert s.data_dir.is_dir()


def test_gemini_model_alias_feeds_live_route(make_settings):
    s = make_settings(GEMINI_MODEL="gemini-x-live")
    assert s.gemini_model == "gemini-x-live"
    assert s.llm_routes["live"] == "gemini:gemini-x-live"


def test_explicit_live_route_beats_alias(make_settings):
    s = make_settings(GEMINI_MODEL="old", FRIDAY_LLM_LIVE="gemini:new")
    assert s.llm_routes["live"] == "gemini:new"


@pytest.mark.parametrize(
    "key,value",
    [
        ("FRIDAY_SENTINEL_BIND", "nocolon"),
        ("FRIDAY_SENTINEL_BIND", ":8770"),
        ("FRIDAY_SENTINEL_BIND", "host:notaport"),
        ("FRIDAY_DB_SYNCHRONOUS", "OFF"),
        ("FRIDAY_TELEMETRY_INTERVAL", "fast"),
        ("FRIDAY_HEARTBEAT_INTERVAL", "-1"),
        ("FRIDAY_RETENTION_DAYS", "2.5"),
    ],
)
def test_invalid_values_name_the_variable(make_settings, key, value):
    with pytest.raises(ConfigError) as excinfo:
        make_settings(**{key: value})
    assert key in str(excinfo.value)


def test_get_settings_is_cached(monkeypatch, tmp_path):
    from friday.core import config

    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "d"))
    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is config.get_settings()
        assert config.get_settings().data_dir == (tmp_path / "d").resolve()
    finally:
        config.get_settings.cache_clear()
```

Append to `tests/conftest.py`:
```python
from pathlib import Path

import pytest

from friday.core.config import Settings, load_settings


@pytest.fixture
def make_settings(tmp_path: Path):
    """Build Settings from an explicit env dict, isolated from the real .env."""

    def _make(**env: str) -> Settings:
        env.setdefault("FRIDAY_DATA_DIR", str(tmp_path / "data"))
        return load_settings(env=env, env_file=tmp_path / "absent.env")

    return _make
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_config.py -v`
Expected: errors — `ModuleNotFoundError: friday.core.config`.

- [ ] **Step 3: Implement**

`friday/core/config.py`:
```python
"""Process configuration for every FRIDAY node.

One frozen ``Settings`` object, built once from the process environment layered
over ``<repo>/.env``. The process environment always wins, so a systemd unit's
``Environment=`` lines override the file. Nothing here reads hardware or
touches the network; ``load_settings`` is a pure function of its inputs apart
from creating ``data_dir``.
"""

from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every LLM call site names a role; the role maps to "provider:model" via env.
LLM_ROLES = ("live", "agent_os", "agent_spatial", "widget", "triage")
DEFAULT_LLM_ROUTES: Mapping[str, str] = {
    "live": "gemini:gemini-3.1-flash-live-preview",
    "agent_os": "gemini:gemini-3.8-flash",
    "agent_spatial": "gemini:gemini-3.8-flash",
    "widget": "gemini:gemini-3.7-flash",
    "triage": "gemini:gemini-3.7-flash",
}

SYNC_MODES = ("FULL", "NORMAL")


class ConfigError(ValueError):
    """A setting is missing or malformed. The message names the variable."""


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    data_dir: Path
    node_id: str
    sentinel_bind_host: str
    sentinel_bind_port: int
    sentinel_url: str | None
    sentinel_token: str | None
    db_synchronous: str
    telemetry_interval_s: float
    heartbeat_interval_s: float
    retention_days: int
    log_level: str
    bridges: tuple[str, ...]
    llm_routes: Mapping[str, str]
    gemini_api_key: str | None
    gemini_model: str | None
    friday_voice: str
    tripo_api_key: str | None


def load_settings(env: Mapping[str, str] | None = None,
                  env_file: Path | None = None) -> Settings:
    """Build Settings. ``env`` defaults to os.environ; ``env_file`` to <repo>/.env."""
    merged = _merge(os.environ if env is None else env, env_file)

    data_dir = Path(merged.get("FRIDAY_DATA_DIR") or REPO_ROOT / "data").expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    host, port = _parse_bind(merged.get("FRIDAY_SENTINEL_BIND", "127.0.0.1:8770"))

    routes = dict(DEFAULT_LLM_ROUTES)
    gemini_model = _optional(merged, "GEMINI_MODEL")
    if gemini_model:
        # Legacy alias: GEMINI_MODEL predates per-role routing and still means
        # "the Live model". An explicit FRIDAY_LLM_LIVE below overrides it.
        routes["live"] = f"gemini:{gemini_model}"
    for role in LLM_ROLES:
        value = _optional(merged, f"FRIDAY_LLM_{role.upper()}")
        if value:
            routes[role] = value

    return Settings(
        repo_root=REPO_ROOT,
        data_dir=data_dir,
        node_id=merged.get("FRIDAY_NODE_ID") or _platform.node() or "friday-node",
        sentinel_bind_host=host,
        sentinel_bind_port=port,
        sentinel_url=_optional(merged, "FRIDAY_SENTINEL_URL"),
        sentinel_token=_optional(merged, "FRIDAY_SENTINEL_TOKEN"),
        db_synchronous=_sync_mode(merged.get("FRIDAY_DB_SYNCHRONOUS", "FULL")),
        telemetry_interval_s=_positive_float(merged, "FRIDAY_TELEMETRY_INTERVAL", 15.0),
        heartbeat_interval_s=_positive_float(merged, "FRIDAY_HEARTBEAT_INTERVAL", 30.0),
        retention_days=_int(merged, "FRIDAY_RETENTION_DAYS", 14),
        log_level=(merged.get("FRIDAY_LOG_LEVEL") or "INFO").upper(),
        bridges=_csv(merged.get("FRIDAY_BRIDGES", "")),
        llm_routes=routes,
        gemini_api_key=_optional(merged, "GEMINI_API_KEY"),
        gemini_model=gemini_model,
        friday_voice=merged.get("FRIDAY_VOICE") or "Aoede",
        tripo_api_key=_optional(merged, "TRIPO_API_KEY"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings for modules that cannot be handed one explicitly."""
    return load_settings()


# ---------------------------------------------------------------- helpers


def _merge(env: Mapping[str, str], env_file: Path | None) -> dict[str, str]:
    path = env_file if env_file is not None else REPO_ROOT / ".env"
    merged: dict[str, str] = {}
    if path.is_file():
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(env)          # process environment always wins
    return merged


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = (env.get(key) or "").strip()
    return value or None


def _parse_bind(spec: str) -> tuple[str, int]:
    host, sep, port = spec.rpartition(":")
    if not sep or not host:
        raise ConfigError(f"FRIDAY_SENTINEL_BIND must be host:port, got {spec!r}")
    try:
        return host, int(port)
    except ValueError as e:
        raise ConfigError(f"FRIDAY_SENTINEL_BIND port must be an integer, got {port!r}") from e


def _sync_mode(value: str) -> str:
    mode = value.strip().upper()
    if mode not in SYNC_MODES:
        raise ConfigError(f"FRIDAY_DB_SYNCHRONOUS must be one of {SYNC_MODES}, got {value!r}")
    return mode


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number of seconds, got {raw!r}") from e
    if value <= 0:
        raise ConfigError(f"{key} must be positive, got {raw!r}")
    return value


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from e


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_config.py -v`
Expected: all PASS.

---

### Task 3: `friday.core.platform`

**Files:**
- Create: `friday/core/platform.py`
- Test: `tests/core/test_platform.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PlatformInfo(system: str, machine: str, arch_family: str, hostname: str, python: str, is_raspberry_pi: bool, distro: str | None)` with `summary` property (`"Darwin/arm64"`) and `to_dict() -> dict`
  - `normalise_arch(machine: str) -> str`
  - `detect(sys_root: Path = Path("/")) -> PlatformInfo` — never raises

- [ ] **Step 1: Write the failing tests**

`tests/core/test_platform.py`:
```python
import json

import pytest

from friday.core.platform import PlatformInfo, detect, normalise_arch


@pytest.mark.parametrize(
    "machine,expected",
    [("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64"), ("armv7l", "other")],
)
def test_normalise_arch(machine, expected):
    assert normalise_arch(machine) == expected


def test_detect_on_this_host():
    info = detect()
    assert isinstance(info, PlatformInfo)
    assert info.system
    assert info.hostname
    assert info.python
    assert info.arch_family in ("arm64", "x86_64", "other")
    assert info.summary == f"{info.system}/{info.arch_family}"
    json.dumps(info.to_dict())


def test_raspberry_pi_detected_from_device_tree(tmp_path):
    model = tmp_path / "proc" / "device-tree" / "model"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"Raspberry Pi 4 Model B Rev 1.4\x00")
    assert detect(sys_root=tmp_path).is_raspberry_pi is True


def test_not_pi_when_model_absent(tmp_path):
    assert detect(sys_root=tmp_path).is_raspberry_pi is False


def test_distro_from_os_release(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "os-release").write_text(
        'NAME="Fedora Linux"\nPRETTY_NAME="Fedora Linux 42 (Server Edition)"\nID=fedora\n'
    )
    assert detect(sys_root=tmp_path).distro == "Fedora Linux 42 (Server Edition)"


def test_distro_none_when_os_release_absent(tmp_path):
    assert detect(sys_root=tmp_path).distro is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_platform.py -v`
Expected: `ModuleNotFoundError: friday.core.platform`.

- [ ] **Step 3: Implement**

`friday/core/platform.py`:
```python
"""Where am I running? Feature probes only; never an OS name comparison.

Every probe takes ``sys_root`` so tests can point it at a fake filesystem, and
every probe swallows its own errors: a node must never fail to boot because a
sysfs file has an unexpected shape.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformInfo:
    system: str          # "Darwin", "Linux", ...
    machine: str         # raw platform.machine()
    arch_family: str     # "arm64" | "x86_64" | "other"
    hostname: str
    python: str
    is_raspberry_pi: bool
    distro: str | None   # PRETTY_NAME from /etc/os-release; None where absent

    @property
    def summary(self) -> str:
        return f"{self.system}/{self.arch_family}"

    def to_dict(self) -> dict:
        return asdict(self)


def normalise_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return "other"


def detect(sys_root: Path = Path("/")) -> PlatformInfo:
    machine = _safe(_platform.machine, "unknown")
    return PlatformInfo(
        system=_safe(_platform.system, "unknown"),
        machine=machine,
        arch_family=normalise_arch(machine),
        hostname=_safe(_platform.node, "unknown") or "unknown",
        python=_safe(_platform.python_version, "unknown"),
        is_raspberry_pi=_is_raspberry_pi(sys_root),
        distro=_distro(sys_root),
    )


def _safe(fn, default: str) -> str:
    try:
        return fn() or default
    except Exception:
        return default


def _is_raspberry_pi(sys_root: Path) -> bool:
    try:
        text = (sys_root / "proc" / "device-tree" / "model").read_text(errors="ignore")
    except OSError:
        return False
    return "raspberry pi" in text.lower()


def _distro(sys_root: Path) -> str | None:
    try:
        lines = (sys_root / "etc" / "os-release").read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"') or None
    return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_platform.py -v`
Expected: all PASS.

---

### Task 4: `friday.core.events`

**Files:**
- Create: `friday/core/events.py`
- Test: `tests/core/test_events.py`

**Interfaces:**
- Consumes: `PlatformInfo` (Task 3)
- Produces:
  - `EVENT_TYPE_RE`, `class EventValidationError(ValueError)`
  - `@dataclass(frozen=True) class Event(type: str, source: str, payload: Mapping = {}, id: str = uuid4 hex, ts: float = now, priority: int = 0)` with `validate() -> None`, `to_dict() -> dict`, `Event.from_dict(d) -> Event`
  - `@dataclass(frozen=True) class Heartbeat(status: str, version: str, platform: str, meta: Mapping = {})` with `to_dict()` / `from_dict()`
  - `@dataclass(frozen=True) class PowerInfo(battery_percent: float | None, on_ac: bool | None, throttled_flags: int | None, under_voltage: bool | None)`
  - `@dataclass(frozen=True) class TelemetrySnapshot(ts, node_id, platform: PlatformInfo, cpu_percent, load_avg, mem_total, mem_used, mem_available, disk_total, disk_used, disk_free, disk_path: str, uptime_s, thermal: Mapping[str, float], power: PowerInfo | None)` with `to_dict()`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_events.py`:
```python
import json

import pytest

from friday.core.events import Event, EventValidationError, Heartbeat, PowerInfo, TelemetrySnapshot
from friday.core.platform import detect


def test_event_defaults():
    e = Event(type="node.heartbeat", source="mac")
    assert len(e.id) == 32
    assert e.ts > 0
    assert e.priority == 0
    assert e.payload == {}


def test_roundtrip():
    e = Event(type="a.b", source="s", payload={"x": 1, "y": [1, 2]}, priority=2)
    assert Event.from_dict(e.to_dict()) == e
    assert Event.from_dict(json.loads(json.dumps(e.to_dict()))) == e


def test_from_dict_fills_defaults_and_ignores_extras():
    e = Event.from_dict({"type": "a.b", "source": "s", "unknown": 1})
    assert e.id and e.ts > 0 and e.priority == 0 and e.payload == {}


def test_from_dict_null_payload_is_empty():
    assert Event.from_dict({"type": "a.b", "source": "s", "payload": None}).payload == {}


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        {"type": "nodots", "source": "s"},
        {"type": "Upper.Case", "source": "s"},
        {"type": "a.b", "source": ""},
        {"type": "a.b"},
        {"source": "s"},
        {"type": "a.b", "source": "s", "payload": "notadict"},
        {"type": "a.b", "source": "s", "ts": "soon"},
        {"type": "a.b", "source": "s", "priority": "high"},
        {"type": "a.b", "source": "s", "id": ""},
    ],
)
def test_invalid_events(bad):
    with pytest.raises(EventValidationError):
        Event.from_dict(bad)


def test_non_json_payload_rejected():
    with pytest.raises(EventValidationError):
        Event(type="a.b", source="s", payload={"o": object()}).validate()


def test_heartbeat_roundtrip():
    hb = Heartbeat(status="listening", version="0.1.0", platform="Darwin/arm64", meta={"k": 1})
    assert Heartbeat.from_dict(hb.to_dict()) == hb


def test_heartbeat_requires_status():
    with pytest.raises(EventValidationError):
        Heartbeat.from_dict({"version": "1", "platform": "x"})
    with pytest.raises(EventValidationError):
        Heartbeat.from_dict("nope")


def test_snapshot_to_dict_is_json_safe():
    snap = TelemetrySnapshot(
        ts=1.0, node_id="n", platform=detect(), cpu_percent=None, load_avg=(1.0, 2.0, 3.0),
        mem_total=None, mem_used=None, mem_available=None, disk_total=None, disk_used=None,
        disk_free=None, disk_path="/", uptime_s=None, thermal={"cpu": 41.5},
        power=PowerInfo(None, None, None, None),
    )
    d = snap.to_dict()
    json.dumps(d)
    assert d["platform"]["system"]
    assert d["thermal"] == {"cpu": 41.5}
    assert d["power"]["under_voltage"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_events.py -v`
Expected: `ModuleNotFoundError: friday.core.events`.

- [ ] **Step 3: Implement**

`friday/core/events.py`:
```python
"""Shared schemas: the Event envelope every node speaks, and the payload
shapes of the two event types the sentinel handles itself.

Event types are dotted lowercase (``node.heartbeat``, ``telemetry.sample``,
``email.received``); handlers match them with glob patterns.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from friday.core.platform import PlatformInfo

EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


class EventValidationError(ValueError):
    """An event (or a typed payload) does not match its schema."""


@dataclass(frozen=True)
class Event:
    type: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    priority: int = 0

    def validate(self) -> None:
        if not isinstance(self.type, str) or not EVENT_TYPE_RE.match(self.type):
            raise EventValidationError(
                f"event type {self.type!r} must be dotted lowercase, e.g. 'node.heartbeat'")
        if not isinstance(self.source, str) or not self.source:
            raise EventValidationError("event source must be a non-empty string")
        if not isinstance(self.id, str) or not self.id:
            raise EventValidationError("event id must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise EventValidationError("event payload must be an object")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise EventValidationError("event ts must be a number")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise EventValidationError("event priority must be an integer")
        try:
            json.dumps(dict(self.payload))
        except (TypeError, ValueError) as e:
            raise EventValidationError(f"event payload is not JSON-serialisable: {e}") from e

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "source": self.source,
            "type": self.type,
            "payload": dict(self.payload),
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Event":
        if not isinstance(d, Mapping):
            raise EventValidationError("event must be a JSON object")
        for key in ("type", "source"):
            if key not in d:
                raise EventValidationError(f"event is missing {key!r}")
        payload = d.get("payload")
        optional = {k: d[k] for k in ("id", "ts", "priority") if k in d}
        try:
            event = cls(type=d["type"], source=d["source"],
                        payload={} if payload is None else payload, **optional)
        except TypeError as e:
            raise EventValidationError(str(e)) from e
        event.validate()
        return event


@dataclass(frozen=True)
class Heartbeat:
    """Payload of ``node.heartbeat``."""

    status: str                 # free text: "listening", "running", "booting", ...
    version: str
    platform: str               # PlatformInfo.summary, e.g. "Linux/arm64"
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "version": self.version,
                "platform": self.platform, "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Heartbeat":
        if not isinstance(d, Mapping) or "status" not in d:
            raise EventValidationError("heartbeat payload must be an object with a 'status'")
        try:
            return cls(status=str(d["status"]), version=str(d.get("version", "")),
                       platform=str(d.get("platform", "")), meta=dict(d.get("meta") or {}))
        except (TypeError, ValueError) as e:
            raise EventValidationError(f"invalid heartbeat payload: {e}") from e


@dataclass(frozen=True)
class PowerInfo:
    battery_percent: float | None
    on_ac: bool | None
    throttled_flags: int | None      # raw `vcgencmd get_throttled` bitmask (Pi only)
    under_voltage: bool | None


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Payload of ``telemetry.sample``. Every probe field is None when unavailable."""

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
    thermal: Mapping[str, float]
    power: PowerInfo | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["thermal"] = dict(self.thermal)
        return d
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_events.py -v`
Expected: all PASS.

---

### Task 5: `friday.core.storage` — synchronous `Store`

**Files:**
- Create: `friday/core/storage.py`
- Test: `tests/core/test_storage.py`

**Interfaces:**
- Consumes: `ConfigError` (Task 2), `Event` (Task 4)
- Produces:
  - `@dataclass(frozen=True) class HeartbeatRow(node_id: str, last_seen: float, status: str, meta: dict)`
  - `@dataclass(frozen=True) class StoredEvent(event: Event, status: str, attempts: int, error: str | None, claimed_at: float | None, processed_at: float | None)`
  - `EVENT_STATUSES = ("pending", "processing", "done", "failed")`
  - `class Store` with: `Store.open(path: Path, *, synchronous: str = "FULL") -> Store`, `close()`, `checkpoint(mode: str = "TRUNCATE")`, `pragma(name: str) -> Any`, `schema_version() -> int`, `connection` property, `kv_get(key) -> Any | None`, `kv_set(key, value)`, `kv_delete(key)`, `heartbeat_upsert(node_id, status, meta: dict, ts: float)`, `heartbeats() -> list[HeartbeatRow]`, `enqueue(event: Event)`, `claim(limit: int, now: float) -> list[StoredEvent]`, `complete(event_id, now)`, `fail(event_id, error: str, now, *, retry: bool)`, `requeue_stale(older_than_s: float, now: float) -> int`, `queue_depths() -> dict[str, int]`, `list_events(*, type=None, status=None, limit=100) -> list[StoredEvent]`, `telemetry_insert(node_id, snapshot: dict, ts)`, `telemetry_latest(node_id=None) -> dict | None`, `telemetry_count(node_id=None) -> int`, `prune(older_than_ts: float) -> dict[str, int]`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_storage.py`:
```python
import time

import pytest

from friday.core.config import ConfigError
from friday.core.events import Event, EventValidationError
from friday.core.storage import Store, StoredEvent


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_open_applies_pragmas(tmp_path):
    s = Store.open(tmp_path / "a.db")
    try:
        assert s.pragma("journal_mode") == "wal"
        assert s.pragma("synchronous") == 2          # FULL
        assert s.pragma("foreign_keys") == 1
        assert s.pragma("busy_timeout") == 5000
        assert s.pragma("journal_size_limit") == 67108864
    finally:
        s.close()


def test_open_normal_sync(tmp_path):
    s = Store.open(tmp_path / "a.db", synchronous="normal")
    try:
        assert s.pragma("synchronous") == 1          # NORMAL
    finally:
        s.close()


def test_open_rejects_unknown_sync(tmp_path):
    with pytest.raises(ConfigError):
        Store.open(tmp_path / "a.db", synchronous="OFF")


def test_open_creates_parent_and_schema(tmp_path):
    s = Store.open(tmp_path / "deep" / "er" / "t.db")
    try:
        assert s.schema_version() == 1
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schema_version", "kv", "heartbeats", "events", "telemetry"} <= names
    finally:
        s.close()


def test_reopen_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    Store.open(p).close()
    s = Store.open(p)
    try:
        assert s.schema_version() == 1
    finally:
        s.close()


def test_kv_roundtrip(store):
    assert store.kv_get("missing") is None
    store.kv_set("k", {"a": [1, 2]})
    assert store.kv_get("k") == {"a": [1, 2]}
    store.kv_set("k", 5)
    assert store.kv_get("k") == 5
    store.kv_delete("k")
    assert store.kv_get("k") is None


def test_heartbeat_upsert(store):
    store.heartbeat_upsert("mac", "listening", {"v": "1"}, 100.0)
    store.heartbeat_upsert("mac", "idle", {"v": "2"}, 200.0)
    store.heartbeat_upsert("pi", "running", {}, 150.0)
    rows = {r.node_id: r for r in store.heartbeats()}
    assert set(rows) == {"mac", "pi"}
    assert rows["mac"].status == "idle"
    assert rows["mac"].last_seen == 200.0
    assert rows["mac"].meta == {"v": "2"}


def test_enqueue_claim_complete(store):
    e = Event(type="a.b", source="s", payload={"n": 1})
    store.enqueue(e)
    assert store.queue_depths() == {"pending": 1, "processing": 0, "done": 0, "failed": 0}

    claimed = store.claim(10, now=1000.0)
    assert len(claimed) == 1
    got = claimed[0]
    assert isinstance(got, StoredEvent)
    assert got.event == e
    assert got.status == "processing" and got.attempts == 1 and got.claimed_at == 1000.0
    assert store.claim(10, now=1001.0) == []

    store.complete(e.id, now=1002.0)
    assert store.queue_depths()["done"] == 1
    assert store.list_events(status="done")[0].processed_at == 1002.0


def test_enqueue_validates(store):
    with pytest.raises(EventValidationError):
        store.enqueue(Event(type="bad", source="s"))


def test_claim_orders_by_priority_then_time(store):
    store.enqueue(Event(type="a.b", source="s", ts=3.0, priority=0, id="low-late"))
    store.enqueue(Event(type="a.b", source="s", ts=1.0, priority=0, id="low-early"))
    store.enqueue(Event(type="a.b", source="s", ts=2.0, priority=5, id="high"))
    assert [c.event.id for c in store.claim(10, now=10.0)] == ["high", "low-early", "low-late"]


def test_claim_respects_limit(store):
    for i in range(5):
        store.enqueue(Event(type="a.b", source="s", ts=float(i)))
    assert len(store.claim(2, now=10.0)) == 2
    assert store.queue_depths()["pending"] == 3


def test_fail_retry_then_terminal(store):
    e = Event(type="a.b", source="s")
    store.enqueue(e)
    store.claim(1, now=1.0)
    store.fail(e.id, "boom", now=2.0, retry=True)
    assert store.queue_depths()["pending"] == 1

    again = store.claim(1, now=3.0)
    assert again[0].attempts == 2
    assert again[0].error == "boom"

    store.fail(e.id, "boom again", now=4.0, retry=False)
    assert store.queue_depths()["failed"] == 1
    failed = store.list_events(status="failed")[0]
    assert failed.error == "boom again" and failed.processed_at == 4.0


def test_requeue_stale(store):
    e = Event(type="a.b", source="s")
    store.enqueue(e)
    store.claim(1, now=100.0)
    assert store.requeue_stale(older_than_s=60.0, now=120.0) == 0   # claimed 20 s ago
    assert store.requeue_stale(older_than_s=0.0, now=120.0) == 1
    assert store.queue_depths()["pending"] == 1


def test_prune_keeps_live_queue(store):
    done = Event(type="a.b", source="s")
    store.enqueue(done)
    store.claim(1, now=1.0)
    store.complete(done.id, now=1.0)
    store.enqueue(Event(type="a.b", source="s"))              # stays pending
    store.telemetry_insert("n", {"cpu": 1}, ts=1.0)
    store.telemetry_insert("n", {"cpu": 2}, ts=time.time() + 100)

    counts = store.prune(older_than_ts=time.time() + 10)
    assert counts == {"telemetry": 1, "events": 1}
    assert store.queue_depths() == {"pending": 1, "processing": 0, "done": 0, "failed": 0}
    assert store.telemetry_latest("n") == {"cpu": 2}


def test_telemetry_latest(store):
    assert store.telemetry_latest() is None
    store.telemetry_insert("a", {"v": 1}, ts=1.0)
    store.telemetry_insert("b", {"v": 2}, ts=2.0)
    store.telemetry_insert("a", {"v": 3}, ts=3.0)
    assert store.telemetry_latest() == {"v": 3}
    assert store.telemetry_latest("b") == {"v": 2}
    assert store.telemetry_latest("zzz") is None
    assert store.telemetry_count() == 3
    assert store.telemetry_count("a") == 2


def test_list_events_filters(store):
    store.enqueue(Event(type="x.y", source="s"))
    store.enqueue(Event(type="x.z", source="s"))
    assert [r.event.type for r in store.list_events(type="x.z")] == ["x.z"]
    assert len(store.list_events()) == 2
    assert len(store.list_events(limit=1)) == 1


def test_corrupt_file_is_quarantined(tmp_path):
    p = tmp_path / "t.db"
    p.write_bytes(b"this is not a database " * 100)
    s = Store.open(p)
    try:
        assert s.schema_version() == 1
        s.kv_set("k", 1)
        assert s.kv_get("k") == 1
    finally:
        s.close()
    quarantined = [q for q in tmp_path.glob("t.db.corrupt-*") if not q.name.endswith(("-wal", "-shm"))]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes().startswith(b"this is not")


def test_checkpoint_truncates_wal(tmp_path):
    p = tmp_path / "t.db"
    s = Store.open(p)
    try:
        for i in range(50):
            s.kv_set(f"k{i}", i)
        wal = tmp_path / "t.db-wal"
        assert wal.exists() and wal.stat().st_size > 0
        s.checkpoint("TRUNCATE")
        assert wal.stat().st_size == 0
    finally:
        s.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_storage.py -v`
Expected: `ModuleNotFoundError: friday.core.storage`.

- [ ] **Step 3: Implement**

`friday/core/storage.py`:
```python
"""Embedded SQLite persistence: state cache, node heartbeats, the durable
event queue and a telemetry log.

Power-loss posture: WAL journal (a torn write can only ever affect the WAL,
never the main file) plus ``synchronous=FULL`` by default (every commit is
fsynced, so a committed event survives a cut). ``NORMAL`` is an opt-in for
SD-card installs: still corruption-safe, may lose the last few commits.

``Store`` is synchronous and single-connection. ``AsyncStore`` (Task 6)
serialises every call onto one dedicated thread.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from friday.core.config import ConfigError
from friday.core.events import Event

log = logging.getLogger(__name__)

MIN_SQLITE = (3, 35, 0)                     # UPDATE ... RETURNING
SYNC_MODES = ("FULL", "NORMAL")
CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
EVENT_STATUSES = ("pending", "processing", "done", "failed")

# Forward-only migrations. Each entry is a tuple of single statements (never
# executescript(): it issues an implicit COMMIT first) applied in one
# transaction together with the schema_version bump.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE kv ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)",
        "CREATE TABLE heartbeats ("
        "  node_id TEXT PRIMARY KEY, last_seen REAL NOT NULL,"
        "  status TEXT NOT NULL, meta TEXT NOT NULL)",
        "CREATE TABLE events ("
        "  id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,"
        "  type TEXT NOT NULL, payload TEXT NOT NULL,"
        "  priority INTEGER NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  created_at REAL NOT NULL, claimed_at REAL, processed_at REAL, error TEXT)",
        "CREATE INDEX events_status_ts ON events(status, priority DESC, ts)",
        "CREATE TABLE telemetry ("
        "  ts REAL NOT NULL, node_id TEXT NOT NULL, snapshot TEXT NOT NULL)",
        "CREATE INDEX telemetry_node_ts ON telemetry(node_id, ts DESC)",
    ),
}

_EVENT_COLUMNS = "id, ts, source, type, payload, priority, status, attempts, error, claimed_at, processed_at"


@dataclass(frozen=True)
class HeartbeatRow:
    node_id: str
    last_seen: float
    status: str
    meta: dict[str, Any]


@dataclass(frozen=True)
class StoredEvent:
    event: Event
    status: str
    attempts: int
    error: str | None
    claimed_at: float | None
    processed_at: float | None


def _row_to_stored(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        event=Event(id=row["id"], ts=row["ts"], source=row["source"], type=row["type"],
                    payload=json.loads(row["payload"]), priority=row["priority"]),
        status=row["status"], attempts=row["attempts"], error=row["error"],
        claimed_at=row["claimed_at"], processed_at=row["processed_at"],
    )


def _quarantine(path: Path) -> None:
    stamp = int(time.time())
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{path}{suffix}")
        if src.exists():
            dst = Path(f"{path}.corrupt-{stamp}{suffix}")
            src.rename(dst)
            log.error("quarantined corrupt database file %s -> %s", src, dst)


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self._conn = conn
        self.path = path

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, path: Path, *, synchronous: str = "FULL") -> "Store":
        synchronous = str(synchronous).upper()
        if synchronous not in SYNC_MODES:
            raise ConfigError(
                f"FRIDAY_DB_SYNCHRONOUS must be one of {SYNC_MODES}, got {synchronous!r}")
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise ConfigError(
                f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ is required (RETURNING support); "
                f"this Python links {sqlite3.sqlite_version}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn: sqlite3.Connection | None = None
        healthy = False
        try:
            conn = cls._connect(path, synchronous)
            healthy = cls._quick_check(conn)
        except sqlite3.DatabaseError as e:
            log.error("database %s is unreadable: %s", path, e)
        if not healthy:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            _quarantine(path)
            conn = cls._connect(path, synchronous)

        store = cls(conn, path)
        store._migrate()
        return store

    @staticmethod
    def _connect(path: Path, synchronous: str) -> sqlite3.Connection:
        # isolation_level=None: autocommit; transactions are explicit BEGIN/COMMIT.
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={synchronous}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    @staticmethod
    def _quick_check(conn: sqlite3.Connection) -> bool:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return row is not None and row[0] == "ok"

    def _migrate(self) -> None:
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = c.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            c.execute("INSERT INTO schema_version (version) VALUES (0)")
            current = 0
        else:
            current = int(row[0])
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            c.execute("BEGIN")
            try:
                for statement in MIGRATIONS[version]:
                    c.execute(statement)
                c.execute("UPDATE schema_version SET version = ?", (version,))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            log.info("database schema migrated to v%d", version)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def schema_version(self) -> int:
        return int(self._conn.execute("SELECT version FROM schema_version").fetchone()[0])

    def pragma(self, name: str) -> Any:
        return self._conn.execute(f"PRAGMA {name}").fetchone()[0]

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        mode = mode.upper()
        if mode not in CHECKPOINT_MODES:
            raise ValueError(f"checkpoint mode must be one of {CHECKPOINT_MODES}")
        self._conn.execute(f"PRAGMA wal_checkpoint({mode})")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------- kv

    def kv_get(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def kv_set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), time.time()))

    def kv_delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ------------------------------------------------------------ heartbeats

    def heartbeat_upsert(self, node_id: str, status: str, meta: dict, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO heartbeats (node_id, last_seen, status, meta) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen, "
            "status = excluded.status, meta = excluded.meta",
            (node_id, ts, status, json.dumps(meta)))

    def heartbeats(self) -> list[HeartbeatRow]:
        rows = self._conn.execute(
            "SELECT node_id, last_seen, status, meta FROM heartbeats ORDER BY node_id").fetchall()
        return [HeartbeatRow(r["node_id"], r["last_seen"], r["status"], json.loads(r["meta"]))
                for r in rows]

    # ----------------------------------------------------------- event queue

    def enqueue(self, event: Event) -> None:
        event.validate()
        self._conn.execute(
            "INSERT INTO events (id, ts, source, type, payload, priority, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (event.id, event.ts, event.source, event.type,
             json.dumps(dict(event.payload)), event.priority, time.time()))

    def claim(self, limit: int, now: float) -> list[StoredEvent]:
        """Atomically move up to ``limit`` pending events to processing."""
        rows = self._conn.execute(
            "UPDATE events SET status = 'processing', claimed_at = ?, attempts = attempts + 1 "
            "WHERE id IN (SELECT id FROM events WHERE status = 'pending' "
            "             ORDER BY priority DESC, ts LIMIT ?) "
            f"RETURNING {_EVENT_COLUMNS}",
            (now, limit)).fetchall()
        items = [_row_to_stored(r) for r in rows]
        items.sort(key=lambda s: (-s.event.priority, s.event.ts))   # RETURNING order is unspecified
        return items

    def complete(self, event_id: str, now: float) -> None:
        self._conn.execute(
            "UPDATE events SET status = 'done', processed_at = ?, error = NULL WHERE id = ?",
            (now, event_id))

    def fail(self, event_id: str, error: str, now: float, *, retry: bool) -> None:
        if retry:
            self._conn.execute(
                "UPDATE events SET status = 'pending', error = ?, claimed_at = NULL WHERE id = ?",
                (error, event_id))
        else:
            self._conn.execute(
                "UPDATE events SET status = 'failed', error = ?, processed_at = ? WHERE id = ?",
                (error, now, event_id))

    def requeue_stale(self, older_than_s: float, now: float) -> int:
        """Return processing rows claimed at or before ``now - older_than_s`` to pending."""
        cur = self._conn.execute(
            "UPDATE events SET status = 'pending', claimed_at = NULL "
            "WHERE status = 'processing' AND (claimed_at IS NULL OR claimed_at <= ?)",
            (now - older_than_s,))
        return cur.rowcount

    def queue_depths(self) -> dict[str, int]:
        depths = {status: 0 for status in EVENT_STATUSES}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status"):
            depths[row["status"]] = row["n"]
        return depths

    def list_events(self, *, type: str | None = None, status: str | None = None,
                    limit: int = 100) -> list[StoredEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit)).fetchall()
        return [_row_to_stored(r) for r in rows]

    # ------------------------------------------------------------- telemetry

    def telemetry_insert(self, node_id: str, snapshot: dict, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO telemetry (ts, node_id, snapshot) VALUES (?, ?, ?)",
            (ts, node_id, json.dumps(snapshot)))

    def telemetry_latest(self, node_id: str | None = None) -> dict | None:
        if node_id is None:
            row = self._conn.execute(
                "SELECT snapshot FROM telemetry ORDER BY ts DESC LIMIT 1").fetchone()
        else:
            row = self._conn.execute(
                "SELECT snapshot FROM telemetry WHERE node_id = ? ORDER BY ts DESC LIMIT 1",
                (node_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def telemetry_count(self, node_id: str | None = None) -> int:
        if node_id is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0])
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE node_id = ?", (node_id,)).fetchone()[0])

    def prune(self, older_than_ts: float) -> dict[str, int]:
        """Drop old telemetry and finished events. Pending/processing are never touched."""
        telemetry = self._conn.execute(
            "DELETE FROM telemetry WHERE ts < ?", (older_than_ts,)).rowcount
        events = self._conn.execute(
            "DELETE FROM events WHERE status IN ('done', 'failed') AND created_at < ?",
            (older_than_ts,)).rowcount
        return {"telemetry": telemetry, "events": events}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_storage.py -v`
Expected: all PASS. If `test_corrupt_file_is_quarantined` fails because `sqlite3.connect` succeeded on garbage but `PRAGMA journal_mode` raised inside `_connect`, that path is already covered by the `except sqlite3.DatabaseError` around the connect — re-read `Store.open` before changing anything.

---

### Task 6: `friday.core.storage` — `AsyncStore`

**Files:**
- Modify: `friday/core/storage.py` (append)
- Test: `tests/core/test_async_store.py`

**Interfaces:**
- Produces: `class AsyncStore` with `AsyncStore.open(path, *, synchronous="FULL") -> AsyncStore` (classmethod, async), `path` property, `run(fn, *args, **kwargs)` (await any callable on the store thread), async twins of every `Store` method with identical names and parameters, `aclose()` (checkpoint TRUNCATE, close, shut the pool).

- [ ] **Step 1: Write the failing tests**

`tests/core/test_async_store.py`:
```python
import threading
import time

import pytest

from friday.core.config import ConfigError
from friday.core.events import Event
from friday.core.storage import AsyncStore


async def test_every_call_runs_on_one_dedicated_thread(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        t1 = await s.run(threading.get_ident)
        t2 = await s.run(threading.get_ident)
        assert t1 == t2
        assert t1 != threading.get_ident()
        name = await s.run(lambda: threading.current_thread().name)
        assert name.startswith("friday-store")
    finally:
        await s.aclose()


async def test_roundtrip_through_async_api(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        e = Event(type="a.b", source="s")
        await s.enqueue(e)
        claimed = await s.claim(1, time.time())
        assert claimed[0].event == e
        await s.complete(e.id, time.time())
        assert (await s.queue_depths())["done"] == 1
        await s.kv_set("k", 1)
        assert await s.kv_get("k") == 1
        await s.heartbeat_upsert("n", "up", {}, 1.0)
        assert (await s.heartbeats())[0].node_id == "n"
        await s.telemetry_insert("n", {"x": 1}, 1.0)
        assert await s.telemetry_latest("n") == {"x": 1}
        assert s.path == tmp_path / "a.db"
    finally:
        await s.aclose()


async def test_aclose_checkpoints_and_closes(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    for i in range(20):
        await s.kv_set(f"k{i}", i)
    await s.aclose()
    wal = tmp_path / "a.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


async def test_open_failure_propagates(tmp_path):
    with pytest.raises(ConfigError):
        await AsyncStore.open(tmp_path / "a.db", synchronous="OFF")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_async_store.py -v`
Expected: `ImportError: cannot import name 'AsyncStore'`.

- [ ] **Step 3: Implement**

Append to `friday/core/storage.py` (add `import asyncio` and `from concurrent.futures import ThreadPoolExecutor` and `from functools import partial` to the imports block):
```python
class AsyncStore:
    """A ``Store`` driven from asyncio.

    All calls — including ``Store.open`` — run on one dedicated thread, so
    sqlite3's default same-thread check stays on and guards that invariant.
    """

    def __init__(self, store: Store, pool: ThreadPoolExecutor):
        self._store = store
        self._pool = pool

    @classmethod
    async def open(cls, path: Path, *, synchronous: str = "FULL") -> "AsyncStore":
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-store")
        loop = asyncio.get_running_loop()
        try:
            store = await loop.run_in_executor(
                pool, partial(Store.open, path, synchronous=synchronous))
        except BaseException:
            pool.shutdown(wait=False)
            raise
        return cls(store, pool)

    @property
    def path(self) -> Path:
        return self._store.path

    async def run(self, fn, *args, **kwargs):
        """Run any callable on the store thread and await its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))

    async def aclose(self) -> None:
        try:
            await self.run(self._store.checkpoint, "TRUNCATE")
        except Exception:
            log.exception("WAL checkpoint on close failed")
        await self.run(self._store.close)
        self._pool.shutdown(wait=True)

    # Async twins, same names and parameters as Store.
    async def checkpoint(self, mode: str = "TRUNCATE") -> None:
        await self.run(self._store.checkpoint, mode)

    async def schema_version(self) -> int:
        return await self.run(self._store.schema_version)

    async def kv_get(self, key: str) -> Any | None:
        return await self.run(self._store.kv_get, key)

    async def kv_set(self, key: str, value: Any) -> None:
        await self.run(self._store.kv_set, key, value)

    async def kv_delete(self, key: str) -> None:
        await self.run(self._store.kv_delete, key)

    async def heartbeat_upsert(self, node_id: str, status: str, meta: dict, ts: float) -> None:
        await self.run(self._store.heartbeat_upsert, node_id, status, meta, ts)

    async def heartbeats(self) -> list[HeartbeatRow]:
        return await self.run(self._store.heartbeats)

    async def enqueue(self, event: Event) -> None:
        await self.run(self._store.enqueue, event)

    async def claim(self, limit: int, now: float) -> list[StoredEvent]:
        return await self.run(self._store.claim, limit, now)

    async def complete(self, event_id: str, now: float) -> None:
        await self.run(self._store.complete, event_id, now)

    async def fail(self, event_id: str, error: str, now: float, *, retry: bool) -> None:
        await self.run(self._store.fail, event_id, error, now, retry=retry)

    async def requeue_stale(self, older_than_s: float, now: float) -> int:
        return await self.run(self._store.requeue_stale, older_than_s, now)

    async def queue_depths(self) -> dict[str, int]:
        return await self.run(self._store.queue_depths)

    async def list_events(self, *, type: str | None = None, status: str | None = None,
                          limit: int = 100) -> list[StoredEvent]:
        return await self.run(self._store.list_events, type=type, status=status, limit=limit)

    async def telemetry_insert(self, node_id: str, snapshot: dict, ts: float) -> None:
        await self.run(self._store.telemetry_insert, node_id, snapshot, ts)

    async def telemetry_latest(self, node_id: str | None = None) -> dict | None:
        return await self.run(self._store.telemetry_latest, node_id)

    async def telemetry_count(self, node_id: str | None = None) -> int:
        return await self.run(self._store.telemetry_count, node_id)

    async def prune(self, older_than_ts: float) -> dict[str, int]:
        return await self.run(self._store.prune, older_than_ts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_storage.py tests/core/test_async_store.py -v`
Expected: all PASS.

---

### Task 7: `friday.core.telemetry`

**Files:**
- Create: `friday/core/telemetry.py`
- Test: `tests/core/test_telemetry.py`

**Interfaces:**
- Consumes: `TelemetrySnapshot`, `PowerInfo` (Task 4), `detect`, `PlatformInfo` (Task 3)
- Produces: `collect(node_id: str, data_dir: Path, *, platform: PlatformInfo | None = None, sys_root: Path = Path("/")) -> TelemetrySnapshot`; internal probes `_thermal(sys_root) -> dict[str, float]`, `_power() -> PowerInfo | None`, `_vcgencmd_throttled() -> int | None` (tests patch these by name).

- [ ] **Step 1: Write the failing tests**

`tests/core/test_telemetry.py`:
```python
import json
import os
from types import SimpleNamespace

import psutil

from friday.core import telemetry
from friday.core.telemetry import collect


def test_collect_on_this_host(tmp_path):
    snap = collect("node", tmp_path)
    assert snap.node_id == "node"
    assert snap.disk_path == str(tmp_path)
    assert snap.mem_total and snap.mem_total > 0
    assert snap.disk_total and snap.disk_total > 0
    assert snap.cpu_percent is not None
    assert snap.uptime_s is not None and snap.uptime_s > 0
    assert isinstance(snap.thermal, dict)
    json.dumps(snap.to_dict())


def test_collect_survives_every_probe_failing(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("no sensor")

    for name in ("cpu_percent", "virtual_memory", "disk_usage", "boot_time"):
        monkeypatch.setattr(psutil, name, boom)
    monkeypatch.setattr(os, "getloadavg", boom)
    monkeypatch.setattr(telemetry, "_thermal", boom)
    monkeypatch.setattr(telemetry, "_power", boom)

    snap = collect("n", tmp_path)
    assert snap.cpu_percent is None
    assert snap.load_avg is None
    assert snap.mem_total is None and snap.mem_used is None and snap.mem_available is None
    assert snap.disk_total is None and snap.disk_used is None and snap.disk_free is None
    assert snap.uptime_s is None
    assert snap.thermal == {}
    assert snap.power is None
    json.dumps(snap.to_dict())


def test_sysfs_thermal_is_parsed(monkeypatch, tmp_path):
    monkeypatch.setattr(psutil, "sensors_temperatures", lambda *a, **k: {}, raising=False)
    zone = tmp_path / "sys" / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("cpu-thermal\n")
    (zone / "temp").write_text("48312\n")
    broken = tmp_path / "sys" / "class" / "thermal" / "thermal_zone1"
    broken.mkdir()
    (broken / "type").write_text("gpu\n")            # no temp file: skipped
    assert telemetry._thermal(tmp_path) == {"cpu-thermal": 48.312}


def test_psutil_thermal_is_preferred(monkeypatch, tmp_path):
    entry = SimpleNamespace(label="Package id 0", current=55.0)
    unnamed = SimpleNamespace(label="", current=41.0)
    monkeypatch.setattr(psutil, "sensors_temperatures",
                        lambda *a, **k: {"coretemp": [entry, unnamed]}, raising=False)
    assert telemetry._thermal(tmp_path) == {"coretemp/Package id 0": 55.0, "coretemp/1": 41.0}


def test_no_thermal_anywhere_is_empty(monkeypatch, tmp_path):
    monkeypatch.delattr(psutil, "sensors_temperatures", raising=False)
    assert telemetry._thermal(tmp_path) == {}


def test_vcgencmd_is_parsed_when_present(monkeypatch, tmp_path):
    exe = tmp_path / "vcgencmd"
    exe.write_text("#!/bin/sh\necho 'throttled=0x50005'\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert telemetry._vcgencmd_throttled() == 0x50005
    power = telemetry._power()
    assert power is not None
    assert power.throttled_flags == 0x50005
    assert power.under_voltage is True


def test_vcgencmd_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert telemetry._vcgencmd_throttled() is None


def test_power_none_without_battery_or_pi(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None, raising=False)
    assert telemetry._power() is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_telemetry.py -v`
Expected: `ModuleNotFoundError: friday.core.telemetry`.

- [ ] **Step 3: Implement**

`friday/core/telemetry.py`:
```python
"""Lightweight system telemetry that never crashes on a missing sensor.

Every probe is wrapped: a failure yields ``None`` (or ``{}``) for that field
and a one-time DEBUG line. macOS has no thermal zones, a Pi has no battery, a
Fedora box has neither vcgencmd nor a battery — all of those are ordinary.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, TypeVar

import psutil

from friday.core.events import PowerInfo, TelemetrySnapshot
from friday.core.platform import PlatformInfo, detect

log = logging.getLogger(__name__)

T = TypeVar("T")
_reported: set[str] = set()


def _probe(name: str, fn: Callable[[], T], default: T = None) -> T:
    try:
        return fn()
    except Exception as e:
        if name not in _reported:
            _reported.add(name)
            log.debug("telemetry probe %s unavailable: %s", name, e)
        return default


def collect(node_id: str, data_dir: Path, *, platform: PlatformInfo | None = None,
            sys_root: Path = Path("/")) -> TelemetrySnapshot:
    info = platform or detect(sys_root)
    vm = _probe("memory", psutil.virtual_memory)
    du = _probe("disk", lambda: psutil.disk_usage(str(data_dir)))
    return TelemetrySnapshot(
        ts=time.time(),
        node_id=node_id,
        platform=info,
        cpu_percent=_probe("cpu", lambda: float(psutil.cpu_percent(interval=None))),
        load_avg=_probe("load", lambda: tuple(float(x) for x in os.getloadavg())),
        mem_total=vm.total if vm else None,
        mem_used=(vm.total - vm.available) if vm else None,
        mem_available=vm.available if vm else None,
        disk_total=du.total if du else None,
        disk_used=du.used if du else None,
        disk_free=du.free if du else None,
        disk_path=str(data_dir),
        uptime_s=_probe("uptime", lambda: max(0.0, time.time() - psutil.boot_time())),
        thermal=_probe("thermal", lambda: _thermal(sys_root), {}) or {},
        power=_probe("power", _power),
    )


def _thermal(sys_root: Path) -> dict[str, float]:
    """psutil first (Linux with lm-sensors); then raw sysfs (Pi, minimal Linux); else {}."""
    out: dict[str, float] = {}
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is not None:
        try:
            for chip, entries in (sensors() or {}).items():
                for i, entry in enumerate(entries):
                    if entry.current is None:
                        continue
                    out[f"{chip}/{entry.label or i}"] = float(entry.current)
        except Exception:
            out = {}
    if out:
        return out
    base = sys_root / "sys" / "class" / "thermal"
    try:
        zones = sorted(base.glob("thermal_zone*"))
    except OSError:
        return out
    for zone in zones:
        try:
            kind = (zone / "type").read_text().strip() or zone.name
            out[kind] = float((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return out


def _power() -> PowerInfo | None:
    battery = None
    sensors_battery = getattr(psutil, "sensors_battery", None)
    if sensors_battery is not None:
        try:
            battery = sensors_battery()
        except Exception:
            battery = None
    flags = _vcgencmd_throttled()
    if battery is None and flags is None:
        return None
    return PowerInfo(
        battery_percent=float(battery.percent) if battery is not None else None,
        on_ac=bool(battery.power_plugged) if battery is not None else None,
        throttled_flags=flags,
        under_voltage=None if flags is None else bool(flags & 0x1),
    )


def _vcgencmd_throttled() -> int | None:
    """Raspberry Pi firmware throttle flags; bit 0 = under-voltage now."""
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True,
                             timeout=2.0).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    return int(match.group(1), 16) if match else None
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_telemetry.py -v`
Expected: all PASS.

---

### Task 8: `friday.core.logsetup`

**Files:**
- Create: `friday/core/logsetup.py`
- Test: `tests/core/test_logsetup.py`

**Interfaces:**
- Produces: `configure_logging(level: str = "INFO", *, env: Mapping[str, str] | None = None, stream=None) -> None` (idempotent; journald-aware).

- [ ] **Step 1: Write the failing tests**

`tests/core/test_logsetup.py`:
```python
import logging

import pytest

from friday.core.logsetup import configure_logging


def _ours():
    return [h for h in logging.getLogger().handlers if getattr(h, "_friday_handler", False)]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for h in _ours():
        logging.getLogger().removeHandler(h)


def test_configure_is_idempotent():
    configure_logging("INFO", env={})
    configure_logging("DEBUG", env={})
    assert len(_ours()) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_plain_format_has_timestamp():
    configure_logging("INFO", env={})
    assert "asctime" in _ours()[0].formatter._fmt


def test_journal_format_has_no_timestamp():
    configure_logging("INFO", env={"JOURNAL_STREAM": "9:12345"})
    assert "asctime" not in _ours()[0].formatter._fmt


def test_level_is_case_insensitive():
    configure_logging("warning", env={})
    assert logging.getLogger().level == logging.WARNING
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_logsetup.py -v`
Expected: `ModuleNotFoundError: friday.core.logsetup`.

- [ ] **Step 3: Implement**

`friday/core/logsetup.py`:
```python
"""One stdout log handler for every node.

Under systemd, stdout is journald; it stamps every line itself, so the
timestamp is dropped when ``JOURNAL_STREAM`` is present. Calling this twice
replaces the previous handler rather than stacking a second one.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Mapping

PLAIN_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
JOURNAL_FORMAT = "%(levelname)-5s %(name)s: %(message)s"
_MARK = "_friday_handler"


def configure_logging(level: str = "INFO", *, env: Mapping[str, str] | None = None,
                      stream=None) -> None:
    env = os.environ if env is None else env
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(
        JOURNAL_FORMAT if "JOURNAL_STREAM" in env else PLAIN_FORMAT))
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_logsetup.py -v`
Expected: all PASS.

---

### Task 9: `friday.core.llm`

**Files:**
- Create: `friday/core/llm/routing.py`, `friday/core/llm/base.py`, `friday/core/llm/gemini.py`
- Modify: `friday/core/llm/__init__.py`
- Test: `tests/core/test_llm.py`

**Interfaces:**
- Consumes: `Settings`, `ConfigError`, `LLM_ROLES` (Task 2)
- Produces:
  - `routing.Route(provider: str, model: str)`, `routing.parse_route(spec: str, *, variable: str = "FRIDAY_LLM") -> Route`, `routing.resolve(settings, role) -> Route`
  - `base.ToolCall(name: str, args: Mapping)`, `base.LLMResponse(text: str, tool_calls: tuple[ToolCall, ...], raw: Any)`, `base.LLMProvider` Protocol with `name`, `model`, `async generate(prompt, *, system=None, tools=None, temperature=None, timeout_s=60.0) -> LLMResponse`
  - `gemini.GeminiProvider(client, model)`
  - `friday.core.llm.gemini_client(settings) -> genai.Client` (cached per key), `friday.core.llm.get_provider(settings, role) -> LLMProvider`, re-exports `Route`, `resolve`, `parse_route`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_llm.py`:
```python
import asyncio
from types import SimpleNamespace

import pytest

from friday.core.config import ConfigError
from friday.core.llm import gemini_client, get_provider, resolve
from friday.core.llm.base import LLMResponse, ToolCall
from friday.core.llm.gemini import GeminiProvider
from friday.core.llm.routing import Route, parse_route


def test_parse_route():
    assert parse_route("gemini:gemini-3.7-flash") == Route("gemini", "gemini-3.7-flash")
    assert parse_route(" Gemini : m ") == Route("gemini", "m")


@pytest.mark.parametrize("bad", ["", "gemini", ":m", "gemini:", "gemini:   "])
def test_parse_route_bad(bad):
    with pytest.raises(ConfigError) as excinfo:
        parse_route(bad, variable="FRIDAY_LLM_X")
    assert "FRIDAY_LLM_X" in str(excinfo.value)


def test_resolve_roles(make_settings):
    s = make_settings(FRIDAY_LLM_WIDGET="gemini:w")
    assert resolve(s, "widget") == Route("gemini", "w")
    assert resolve(s, "live").model == "gemini-3.1-flash-live-preview"


def test_resolve_unknown_role(make_settings):
    with pytest.raises(ConfigError):
        resolve(make_settings(), "nope")


def test_resolve_bad_spec_names_variable(make_settings):
    s = make_settings(FRIDAY_LLM_TRIAGE="nocolon")
    with pytest.raises(ConfigError) as excinfo:
        resolve(s, "triage")
    assert "FRIDAY_LLM_TRIAGE" in str(excinfo.value)


def test_gemini_client_requires_key(make_settings):
    with pytest.raises(ConfigError) as excinfo:
        gemini_client(make_settings())
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_gemini_client_is_cached_per_key(make_settings):
    s = make_settings(GEMINI_API_KEY="k-test-cache")
    assert gemini_client(s) is gemini_client(s)


def test_get_provider_unknown_provider(make_settings):
    s = make_settings(GEMINI_API_KEY="k", FRIDAY_LLM_TRIAGE="openai:gpt")
    with pytest.raises(ConfigError) as excinfo:
        get_provider(s, "triage")
    assert "openai" in str(excinfo.value) and "FRIDAY_LLM_TRIAGE" in str(excinfo.value)


def test_get_provider_gemini(make_settings):
    p = get_provider(make_settings(GEMINI_API_KEY="k"), "triage")
    assert isinstance(p, GeminiProvider)
    assert p.name == "gemini" and p.model == "gemini-3.7-flash"


class _FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _fake_client(response):
    return SimpleNamespace(aio=SimpleNamespace(models=_FakeModels(response)))


async def test_generate_text():
    response = SimpleNamespace(text="hello", function_calls=None)
    client = _fake_client(response)
    r = await GeminiProvider(client, "m").generate("hi", system="be brief", temperature=0.1)
    assert r == LLMResponse(text="hello", tool_calls=(), raw=response)
    kwargs = client.aio.models.calls[0]
    assert kwargs["model"] == "m" and kwargs["contents"] == "hi"
    assert kwargs["config"].system_instruction == "be brief"
    assert kwargs["config"].temperature == 0.1
    assert kwargs["config"].tools is None


async def test_generate_tool_calls():
    call = SimpleNamespace(name="look", args={"display": "all"})
    client = _fake_client(SimpleNamespace(text=None, function_calls=[call]))
    tools = [{"name": "look", "description": "d",
              "parameters": {"type": "object", "properties": {"display": {"type": "string"}}}}]
    r = await GeminiProvider(client, "m").generate("do", tools=tools)
    assert r.text == ""
    assert r.tool_calls == (ToolCall("look", {"display": "all"}),)
    config = client.aio.models.calls[0]["config"]
    assert config.tools[0].function_declarations[0].name == "look"
    assert config.automatic_function_calling.disable is True


async def test_generate_timeout():
    class Slow:
        async def generate_content(self, **kwargs):
            await asyncio.sleep(1)

    client = SimpleNamespace(aio=SimpleNamespace(models=Slow()))
    with pytest.raises(asyncio.TimeoutError):
        await GeminiProvider(client, "m").generate("x", timeout_s=0.01)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_llm.py -v`
Expected: `ImportError` from `friday.core.llm`.

- [ ] **Step 3: Implement**

`friday/core/llm/routing.py`:
```python
"""Role → (provider, model) routing, driven by FRIDAY_LLM_<ROLE> env vars."""

from __future__ import annotations

from dataclasses import dataclass

from friday.core.config import LLM_ROLES, ConfigError, Settings


@dataclass(frozen=True)
class Route:
    provider: str
    model: str


def parse_route(spec: str, *, variable: str = "FRIDAY_LLM") -> Route:
    provider, sep, model = (spec or "").partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not sep or not provider or not model:
        raise ConfigError(f"{variable} must be 'provider:model', got {spec!r}")
    return Route(provider, model)


def resolve(settings: Settings, role: str) -> Route:
    if role not in LLM_ROLES:
        raise ConfigError(f"unknown LLM role {role!r}; expected one of {LLM_ROLES}")
    return parse_route(settings.llm_routes[role], variable=f"FRIDAY_LLM_{role.upper()}")
```

`friday/core/llm/base.py`:
```python
"""Provider-neutral text generation interface.

Tools are plain JSON-schema function declarations
(``{"name", "description", "parameters"}``) — the shape the hub already
keeps in TOOL_FUNCTION_DECLARATIONS — so a second provider maps the same
input. The Gemini Live audio session is not behind this interface; it is a
provider-specific protocol and stays in friday.desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse: ...
```

`friday/core/llm/gemini.py`:
```python
"""Gemini adapter over google-genai's generate_content."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from google.genai import types

from friday.core.llm.base import LLMResponse, ToolCall


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: Any, model: str):
        self._client = client
        self.model = model

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse:
        config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        if tools:
            config.tools = [types.Tool(function_declarations=[
                types.FunctionDeclaration(**dict(tool)) for tool in tools])]
            # The caller owns tool execution; the SDK must not call anything itself.
            config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(
                model=self.model, contents=prompt, config=config),
            timeout=timeout_s)
        calls = tuple(ToolCall(name=c.name, args=dict(c.args or {}))
                      for c in (getattr(response, "function_calls", None) or []))
        return LLMResponse(text=getattr(response, "text", None) or "",
                           tool_calls=calls, raw=response)
```

`friday/core/llm/__init__.py`:
```python
"""LLM factory: resolve a role to a provider+model from the environment.

Only Gemini ships. ``get_provider`` is the seam a second provider plugs into;
callers never construct a provider directly.
"""

from __future__ import annotations

from typing import Any

from friday.core.config import ConfigError, Settings
from friday.core.llm.base import LLMProvider, LLMResponse, ToolCall
from friday.core.llm.routing import Route, parse_route, resolve

__all__ = ["LLMProvider", "LLMResponse", "Route", "ToolCall", "gemini_client",
           "get_provider", "parse_route", "resolve"]

_gemini_clients: dict[str, Any] = {}


def gemini_client(settings: Settings) -> Any:
    """The process-wide google-genai client (one per API key)."""
    key = settings.gemini_api_key
    if not key:
        raise ConfigError("GEMINI_API_KEY is not set")
    if key not in _gemini_clients:
        from google import genai   # deferred: keeps `import friday.core.llm` cheap
        _gemini_clients[key] = genai.Client(api_key=key)
    return _gemini_clients[key]


def get_provider(settings: Settings, role: str) -> LLMProvider:
    route = resolve(settings, role)
    if route.provider == "gemini":
        from friday.core.llm.gemini import GeminiProvider
        return GeminiProvider(gemini_client(settings), route.model)
    raise ConfigError(
        f"FRIDAY_LLM_{role.upper()}: unknown provider {route.provider!r} (supported: gemini)")
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core -v`
Expected: all PASS across every core test file.

---

### Task 10: `friday.sentinel.handlers`

**Files:**
- Create: `friday/sentinel/handlers.py`
- Test: `tests/sentinel/test_handlers.py`

**Interfaces:**
- Consumes: `Settings` (Task 2), `Event`, `Heartbeat` (Task 4), `AsyncStore` (Task 6)
- Produces:
  - `@dataclass class HandlerContext(settings: Settings, store: AsyncStore, bus: Any, logger: logging.Logger)`
  - `class Handler(Protocol)`: `name: str`, `patterns: Sequence[str]`, `async handle(event: Event, ctx: HandlerContext) -> None`
  - `matches(patterns: Sequence[str], event_type: str) -> bool`
  - `HeartbeatHandler`, `TelemetryHandler`, `LogHandler`

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_handlers.py`:
```python
import logging

import pytest

from friday.core.events import Event, EventValidationError, Heartbeat
from friday.core.storage import AsyncStore
from friday.sentinel.handlers import (HandlerContext, HeartbeatHandler, LogHandler,
                                      TelemetryHandler, matches)


@pytest.fixture
async def ctx(make_settings, tmp_path):
    store = await AsyncStore.open(tmp_path / "t.db")
    context = HandlerContext(settings=make_settings(FRIDAY_NODE_ID="sentinel-test"),
                             store=store, bus=None, logger=logging.getLogger("test.events"))
    yield context
    await store.aclose()


def test_matches():
    assert matches(("*",), "a.b")
    assert matches(("node.*",), "node.heartbeat")
    assert not matches(("node.*",), "telemetry.sample")
    assert matches(("a.b", "c.*"), "c.d")
    assert not matches((), "a.b")


async def test_heartbeat_handler_upserts(ctx):
    hb = Heartbeat(status="listening", version="0.1.0", platform="Darwin/arm64", meta={"ip": "1.2.3.4"})
    event = Event(type="node.heartbeat", source="mac", payload=hb.to_dict(), ts=42.0)
    await HeartbeatHandler().handle(event, ctx)
    rows = await ctx.store.heartbeats()
    assert rows[0].node_id == "mac"
    assert rows[0].status == "listening"
    assert rows[0].last_seen == 42.0
    assert rows[0].meta == {"version": "0.1.0", "platform": "Darwin/arm64", "ip": "1.2.3.4"}


async def test_heartbeat_handler_rejects_bad_payload(ctx):
    with pytest.raises(EventValidationError):
        await HeartbeatHandler().handle(Event(type="node.heartbeat", source="mac", payload={}), ctx)


async def test_telemetry_handler_stores(ctx):
    event = Event(type="telemetry.sample", source="pi", payload={"cpu_percent": 3.0}, ts=5.0)
    await TelemetryHandler().handle(event, ctx)
    assert await ctx.store.telemetry_latest("pi") == {"cpu_percent": 3.0}


async def test_log_handler_logs_type_source_id(ctx, caplog):
    with caplog.at_level(logging.INFO, logger="test.events"):
        await LogHandler().handle(Event(type="x.y", source="src", id="abc123"), ctx)
    assert "x.y" in caplog.text and "src" in caplog.text and "abc123" in caplog.text


def test_builtin_patterns():
    assert HeartbeatHandler().patterns == ("node.heartbeat",)
    assert TelemetryHandler().patterns == ("telemetry.sample",)
    assert LogHandler().patterns == ("*",)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_handlers.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.handlers`.

- [ ] **Step 3: Implement**

`friday/sentinel/handlers.py`:
```python
"""Event handlers: the reactive side of the bus.

A handler declares glob patterns over event types and is awaited for every
matching event. Delivery is at-least-once (a retried event re-runs every
handler), so handlers must be idempotent — an upsert, not an insert.

``LogHandler`` is the triage seam: a future ``TriageHandler`` subscribes to
``*`` exactly like it, calls ``get_provider(settings, "triage")`` and
publishes ``command.*`` events for bridges to act on.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from friday.core.config import Settings
from friday.core.events import Event, Heartbeat
from friday.core.storage import AsyncStore


@dataclass
class HandlerContext:
    settings: Settings
    store: AsyncStore
    bus: Any                    # EventBus; typed loosely to avoid an import cycle
    logger: logging.Logger


class Handler(Protocol):
    name: str
    patterns: Sequence[str]

    async def handle(self, event: Event, ctx: HandlerContext) -> None: ...


def matches(patterns: Sequence[str], event_type: str) -> bool:
    return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in patterns)


class HeartbeatHandler:
    name = "heartbeat"
    patterns = ("node.heartbeat",)

    async def handle(self, event: Event, ctx: HandlerContext) -> None:
        hb = Heartbeat.from_dict(event.payload)
        meta = {"version": hb.version, "platform": hb.platform, **dict(hb.meta)}
        await ctx.store.heartbeat_upsert(event.source, hb.status, meta, event.ts)


class TelemetryHandler:
    name = "telemetry"
    patterns = ("telemetry.sample",)

    async def handle(self, event: Event, ctx: HandlerContext) -> None:
        await ctx.store.telemetry_insert(event.source, dict(event.payload), event.ts)


class LogHandler:
    name = "log"
    patterns = ("*",)

    async def handle(self, event: Event, ctx: HandlerContext) -> None:
        ctx.logger.info("event %s from %s (%s)", event.type, event.source, event.id)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_handlers.py -v`
Expected: all PASS.

---

### Task 11: `friday.sentinel.bus`

**Files:**
- Create: `friday/sentinel/bus.py`
- Test: `tests/sentinel/test_bus.py`

**Interfaces:**
- Consumes: `AsyncStore`, `StoredEvent` (Tasks 5–6), `Event`, `Handler`, `HandlerContext`, `matches` (Task 10)
- Produces: `class EventBus(store, *, max_attempts=3, handler_timeout_s=30.0, batch=16, poll_interval_s=1.0)` with `subscribe(handler)`, `unsubscribe(handler)`, `handlers_for(event_type) -> list[Handler]`, `async publish(event) -> str`, `async run_dispatcher(ctx: HandlerContext) -> None`, `async drain() -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_bus.py`:
```python
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from friday.core.events import Event, EventValidationError
from friday.core.storage import AsyncStore
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext


class Recorder:
    def __init__(self, name="rec", patterns=("*",), fail_times=0, delay=0.0):
        self.name = name
        self.patterns = patterns
        self.seen = []
        self._fail = fail_times
        self.delay = delay

    async def handle(self, event, ctx):
        self.seen.append(event)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("nope")


async def until(predicate, timeout=3.0):
    """Poll an async predicate until true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def rig(make_settings, tmp_path):
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store, max_attempts=3, handler_timeout_s=0.2, poll_interval_s=0.05)
    ctx = HandlerContext(settings=make_settings(), store=store, bus=bus,
                         logger=logging.getLogger("test.bus"))
    task = asyncio.create_task(bus.run_dispatcher(ctx))
    yield SimpleNamespace(bus=bus, store=store, ctx=ctx, task=task)
    await bus.drain()
    await asyncio.wait_for(task, 2)
    await store.aclose()


async def depth(rig, status):
    return (await rig.store.queue_depths())[status]


def depth_is(rig, status, n):
    """Async predicate for until(): queue depth of ``status`` equals ``n``."""
    async def check():
        return await depth(rig, status) == n
    return check


def seen_count(handler, n):
    async def check():
        return len(handler.seen) == n
    return check


async def test_publish_is_durable_then_dispatched(rig):
    h = Recorder()
    rig.bus.subscribe(h)
    event_id = await rig.bus.publish(Event(type="a.b", source="s", payload={"n": 1}))
    await until(depth_is(rig, "done", 1))
    assert [e.id for e in h.seen] == [event_id]


async def test_pattern_matching(rig):
    a, b = Recorder("a", ("node.*",)), Recorder("b", ("telemetry.sample",))
    rig.bus.subscribe(a)
    rig.bus.subscribe(b)
    await rig.bus.publish(Event(type="node.heartbeat", source="s"))
    await rig.bus.publish(Event(type="telemetry.sample", source="s"))
    await until(depth_is(rig, "done", 2))
    assert [e.type for e in a.seen] == ["node.heartbeat"]
    assert [e.type for e in b.seen] == ["telemetry.sample"]


async def test_publish_validates(rig):
    with pytest.raises(EventValidationError):
        await rig.bus.publish(Event(type="bad", source="s"))
    assert await depth(rig, "pending") == 0


async def test_failing_handler_is_retried_then_parked(rig):
    h = Recorder(fail_times=99)
    rig.bus.subscribe(h)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1))
    assert len(h.seen) == 3
    failed = (await rig.store.list_events(status="failed"))[0]
    assert failed.attempts == 3
    assert "rec: RuntimeError: nope" in failed.error


async def test_recovers_on_retry(rig):
    h = Recorder(fail_times=1)
    rig.bus.subscribe(h)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "done", 1))
    assert len(h.seen) == 2


async def test_one_bad_handler_does_not_block_another(rig):
    bad, good = Recorder("bad", fail_times=99), Recorder("good")
    rig.bus.subscribe(bad)
    rig.bus.subscribe(good)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1))
    assert len(good.seen) == 3        # at-least-once: re-run on every attempt


async def test_handler_timeout_is_enforced(rig):
    slow = Recorder(delay=1.0)
    rig.bus.subscribe(slow)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1), timeout=5.0)
    failed = (await rig.store.list_events(status="failed"))[0]
    assert "TimeoutError" in failed.error


async def test_drain_waits_for_in_flight_handler(rig):
    slow = Recorder(delay=0.3)
    rig.bus.subscribe(slow)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(seen_count(slow, 1))
    await rig.bus.drain()                       # returns only once the handler finished
    assert await depth(rig, "done") == 1
    await asyncio.wait_for(rig.task, 1)         # dispatcher exited on its own


def test_subscribe_unsubscribe_and_handlers_for(tmp_path):
    bus = EventBus(store=None)
    a = Recorder("a", ("node.*",))
    bus.subscribe(a)
    assert bus.handlers_for("node.heartbeat") == [a]
    assert bus.handlers_for("x.y") == []
    bus.unsubscribe(a)
    assert bus.handlers_for("node.heartbeat") == []
    bus.unsubscribe(a)                          # idempotent
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_bus.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.bus`.

- [ ] **Step 3: Implement**

`friday/sentinel/bus.py`:
```python
"""Durable event bus.

``publish`` writes the event to SQLite before returning, so an event that was
accepted survives a crash. The dispatcher claims batches, runs every matching
handler, and marks the row done or failed. Retries are immediate (no backoff
column yet) and capped by ``max_attempts``; events are processed one at a
time, so a slow handler delays the queue behind it — both are acceptable at
heartbeat volumes and are the first things to revisit when they are not.
"""

from __future__ import annotations

import asyncio
import logging
import time

from friday.core.events import Event
from friday.core.storage import AsyncStore, StoredEvent
from friday.sentinel.handlers import Handler, HandlerContext, matches

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, store: AsyncStore, *, max_attempts: int = 3,
                 handler_timeout_s: float = 30.0, batch: int = 16,
                 poll_interval_s: float = 1.0):
        self._store = store
        self._max_attempts = max_attempts
        self._handler_timeout_s = handler_timeout_s
        self._batch = batch
        self._poll_interval_s = poll_interval_s
        self._handlers: list[Handler] = []
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False

    # ------------------------------------------------------------ handlers

    def subscribe(self, handler: Handler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def handlers_for(self, event_type: str) -> list[Handler]:
        return [h for h in self._handlers if matches(h.patterns, event_type)]

    # ------------------------------------------------------------- publish

    async def publish(self, event: Event) -> str:
        event.validate()
        await self._store.enqueue(event)
        self._wake.set()
        return event.id

    # ---------------------------------------------------------- dispatcher

    async def run_dispatcher(self, ctx: HandlerContext) -> None:
        try:
            while not self._stopping:
                self._wake.clear()           # before claim: a publish in between still wins
                claimed = await self._store.claim(self._batch, time.time())
                if not claimed:
                    self._idle.set()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_s)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._idle.clear()
                for item in claimed:
                    await self._dispatch_one(item, ctx)
        finally:
            self._idle.set()

    async def drain(self) -> None:
        """Stop the dispatcher after its current batch and wait for it to go idle."""
        self._stopping = True
        self._wake.set()
        await self._idle.wait()

    async def _dispatch_one(self, item: StoredEvent, ctx: HandlerContext) -> None:
        event = item.event
        errors: list[str] = []
        for handler in self.handlers_for(event.type):
            try:
                await asyncio.wait_for(handler.handle(event, ctx), timeout=self._handler_timeout_s)
            except Exception as e:
                errors.append(f"{handler.name}: {type(e).__name__}: {e}")
                log.warning("handler %s failed on %s (%s), attempt %d: %s",
                            handler.name, event.type, event.id, item.attempts, e)
        now = time.time()
        if not errors:
            await self._store.complete(event.id, now)
            return
        retry = item.attempts < self._max_attempts
        await self._store.fail(event.id, "; ".join(errors), now, retry=retry)
        if not retry:
            log.error("event %s (%s) parked as failed after %d attempts",
                      event.type, event.id, item.attempts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_bus.py -v`
Expected: all PASS.

---

### Task 12: `friday.sentinel.sdnotify`

**Files:**
- Create: `friday/sentinel/sdnotify.py`
- Test: `tests/sentinel/test_sdnotify.py`

**Interfaces:**
- Produces: `sd_notify(state: str, *, env: Mapping[str, str] | None = None) -> bool` — True if a datagram was sent; False (never raises) otherwise.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_sdnotify.py`:
```python
import os
import socket
import tempfile

import pytest

from friday.sentinel.sdnotify import sd_notify


def test_noop_without_notify_socket():
    assert sd_notify("READY=1", env={}) is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
def test_sends_datagram():
    with tempfile.TemporaryDirectory() as d:          # short path: AF_UNIX has a ~100-char limit
        path = os.path.join(d, "n.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(2.0)
        try:
            assert sd_notify("READY=1", env={"NOTIFY_SOCKET": path}) is True
            assert server.recv(64) == b"READY=1"
        finally:
            server.close()


def test_unreachable_socket_is_swallowed(tmp_path):
    assert sd_notify("READY=1", env={"NOTIFY_SOCKET": str(tmp_path / "missing.sock")}) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_sdnotify.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.sdnotify`.

- [ ] **Step 3: Implement**

`friday/sentinel/sdnotify.py`:
```python
"""Minimal sd_notify(3): READY=1 / WATCHDOG=1 / STOPPING=1 over NOTIFY_SOCKET.

A no-op anywhere systemd did not start us (macOS, a foreground run), which
is why the daemon can call it unconditionally.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Mapping

log = logging.getLogger(__name__)
_warned = False


def sd_notify(state: str, *, env: Mapping[str, str] | None = None) -> bool:
    global _warned
    env = os.environ if env is None else env
    address = env.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):                 # Linux abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(address)
            sock.sendall(state.encode())
        return True
    except OSError as e:
        if not _warned:
            _warned = True
            log.warning("sd_notify to %s failed: %s", env.get("NOTIFY_SOCKET"), e)
        return False
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_sdnotify.py -v`
Expected: all PASS.

---

### Task 13: `friday.sentinel.api`

**Files:**
- Create: `friday/sentinel/api.py`
- Test: `tests/sentinel/test_api.py`

**Interfaces:**
- Consumes: `Settings`, `Event`, `EventValidationError`, `AsyncStore`, `PlatformInfo`, `EventBus`, `matches`, `HandlerContext`, `friday.__version__`
- Produces:
  - `MAX_BODY_BYTES = 262144`, `MAX_BATCH = 100`
  - `@dataclass class HealthState(started_at: float, platform: PlatformInfo, restarts: dict[str, int] = {})`
  - `class WebSocketFanout` (a `Handler`, `patterns=("*",)`) with `add(ws, patterns=("*",))`, `remove(sub)`, `connections` property, `async close_all(code=1001)`
  - `create_app(settings, bus, store, state) -> aiohttp.web.Application` (subscribes its fanout on `bus`; `app[FANOUT]` exposes it)
  - `class ApiServer(settings, bus, store, state)` with `app`, `fanout`, `port: int | None`, `async start()`, `async stop()`
  - Routes: `GET /health`, `GET /telemetry`, `GET /nodes`, `POST /events`, `GET /ws`

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_api.py`:
```python
import asyncio
import logging
import time
from types import SimpleNamespace

import aiohttp
import pytest

import friday
from friday.core.events import Event, Heartbeat
from friday.core.platform import detect
from friday.core.storage import AsyncStore
from friday.sentinel.api import MAX_BODY_BYTES, ApiServer, HealthState, create_app
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext, HeartbeatHandler, TelemetryHandler

AUTH = {"Authorization": "Bearer secret"}


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def _build(make_settings, tmp_path, **env):
    settings = make_settings(FRIDAY_NODE_ID="sentinel-test", **env)
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store, poll_interval_s=0.05)
    ctx = HandlerContext(settings=settings, store=store, bus=bus, logger=logging.getLogger("test.api"))
    bus.subscribe(HeartbeatHandler())
    bus.subscribe(TelemetryHandler())
    state = HealthState(started_at=time.time(), platform=detect(), restarts={"x": 2})
    app = create_app(settings, bus, store, state)
    task = asyncio.create_task(bus.run_dispatcher(ctx))
    return SimpleNamespace(settings=settings, store=store, bus=bus, app=app, task=task, state=state)


async def _teardown(rig):
    await rig.bus.drain()
    await asyncio.wait_for(rig.task, 2)
    await rig.store.aclose()


@pytest.fixture
async def rig(make_settings, tmp_path):
    r = await _build(make_settings, tmp_path, FRIDAY_SENTINEL_TOKEN="secret")
    yield r
    await _teardown(r)


@pytest.fixture
async def open_rig(make_settings, tmp_path):
    r = await _build(make_settings, tmp_path)
    yield r
    await _teardown(r)


async def test_health(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["node_id"] == "sentinel-test"
    assert body["version"] == friday.__version__
    assert body["uptime_s"] >= 0
    assert body["platform"]["system"]
    assert body["queue"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    assert body["supervisor_restarts"] == {"x": 2}


async def test_telemetry_204_then_200(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    assert (await client.get("/telemetry")).status == 204
    await rig.store.telemetry_insert("sentinel-test", {"cpu_percent": 1.5}, ts=1.0)
    resp = await client.get("/telemetry")
    assert resp.status == 200
    assert (await resp.json())["cpu_percent"] == 1.5


async def test_post_event_requires_token(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    body = {"type": "a.b", "source": "s"}
    assert (await client.post("/events", json=body)).status == 401
    assert (await client.post("/events", json=body, headers={"Authorization": "Bearer wrong"})).status == 401
    assert (await client.post("/events", json=body, headers={"Authorization": "Basic secret"})).status == 401


async def test_post_single_event_and_nodes(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    hb = Heartbeat(status="listening", version="0.1.0", platform="Darwin/arm64")
    resp = await client.post("/events", json={"type": "node.heartbeat", "source": "mac", "payload": hb.to_dict()},
                             headers=AUTH)
    assert resp.status == 202
    ids = (await resp.json())["ids"]
    assert len(ids) == 1 and len(ids[0]) == 32

    async def done():
        return (await rig.store.queue_depths())["done"] == 1
    await until(done)

    nodes = await (await client.get("/nodes")).json()
    assert nodes == [{"node_id": "mac", "last_seen": nodes[0]["last_seen"], "status": "listening",
                      "meta": {"version": "0.1.0", "platform": "Darwin/arm64"}}]


async def test_post_batch(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    resp = await client.post("/events", json=[{"type": "a.b", "source": "s"}, {"type": "a.c", "source": "s"}],
                             headers=AUTH)
    assert resp.status == 202
    assert len((await resp.json())["ids"]) == 2


async def test_invalid_batch_is_rejected_whole(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    resp = await client.post("/events", json=[{"type": "a.b", "source": "s"}, {"type": "BAD", "source": "s"}],
                             headers=AUTH)
    assert resp.status == 400
    assert "event 1" in (await resp.json())["error"]
    depths = await rig.store.queue_depths()
    assert depths["pending"] == 0 and depths["done"] == 0


@pytest.mark.parametrize("body", [b"{not json", b"[]", b'"just a string"', b"[1, 2]"])
async def test_bad_bodies_are_400(aiohttp_client, rig, body):
    client = await aiohttp_client(rig.app)
    resp = await client.post("/events", data=body, headers={**AUTH, "Content-Type": "application/json"})
    assert resp.status == 400
    assert "error" in await resp.json()


async def test_too_many_events_is_400(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    resp = await client.post("/events", json=[{"type": "a.b", "source": "s"}] * 101, headers=AUTH)
    assert resp.status == 400


async def test_oversized_body_is_413(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    big = {"type": "a.b", "source": "s", "payload": {"blob": "x" * (MAX_BODY_BYTES + 1024)}}
    resp = await client.post("/events", json=big, headers=AUTH)
    assert resp.status == 413


async def test_no_token_configured_means_open(aiohttp_client, open_rig):
    client = await aiohttp_client(open_rig.app)
    assert (await client.post("/events", json={"type": "a.b", "source": "s"})).status == 202


async def test_ws_requires_token(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    assert (await client.get("/ws")).status == 401


async def test_ws_streams_only_subscribed_events(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    ws = await client.ws_connect("/ws?token=secret")
    await ws.send_json({"subscribe": ["node.*"]})
    assert await asyncio.wait_for(ws.receive_json(), 2) == {"subscribed": ["node.*"]}

    await rig.bus.publish(Event(type="telemetry.sample", source="s", payload={}))
    await rig.bus.publish(Event(type="node.heartbeat", source="mac",
                                payload=Heartbeat("idle", "0.1.0", "x").to_dict()))
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["type"] == "node.heartbeat" and msg["source"] == "mac"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive_json(), 0.3)
    await ws.close()


async def test_ws_header_auth_and_default_subscription(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    ws = await client.ws_connect("/ws", headers=AUTH)
    await rig.bus.publish(Event(type="anything.goes", source="s"))
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["type"] == "anything.goes"
    await ws.send_str("not json")
    assert "error" in await asyncio.wait_for(ws.receive_json(), 2)
    await ws.close()


async def test_dead_socket_is_dropped_without_failing_the_event(aiohttp_client, rig):
    client = await aiohttp_client(rig.app)
    ws = await client.ws_connect("/ws", headers=AUTH)
    await ws.close()
    await asyncio.sleep(0.05)
    await rig.bus.publish(Event(type="a.b", source="s"))

    async def done():
        return (await rig.store.queue_depths())["done"] == 1
    await until(done)
    assert (await rig.store.queue_depths())["failed"] == 0


async def test_server_start_stop_on_ephemeral_port(make_settings, tmp_path):
    settings = make_settings(FRIDAY_NODE_ID="n", FRIDAY_SENTINEL_BIND="127.0.0.1:0")
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store)
    server = ApiServer(settings, bus, store, HealthState(started_at=time.time(), platform=detect()))
    await server.start()
    try:
        assert server.port and server.port > 0
        assert bus.handlers_for("x.y") == [server.fanout]
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{server.port}/health") as resp:
                assert resp.status == 200
    finally:
        await server.stop()
        await store.aclose()
    assert bus.handlers_for("x.y") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_api.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.api`.

- [ ] **Step 3: Implement**

`friday/sentinel/api.py`:
```python
"""HTTP + WebSocket surface of the sentinel.

Bound to localhost by default and fronted by Tailscale Serve for remote
nodes, exactly like the desktop hub. ``POST /events`` is how any node or
bridge pushes into the bus; ``/ws`` is the push channel out. When
``FRIDAY_SENTINEL_TOKEN`` is set both require it.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

import friday
from friday.core.config import Settings
from friday.core.events import Event, EventValidationError
from friday.core.platform import PlatformInfo
from friday.core.storage import AsyncStore
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext, matches

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
MAX_BATCH = 100


@dataclass
class HealthState:
    started_at: float
    platform: PlatformInfo
    restarts: dict[str, int] = field(default_factory=dict)


@dataclass
class _Subscription:
    ws: web.WebSocketResponse
    patterns: tuple[str, ...]


class WebSocketFanout:
    """Bus handler that pushes every matching event to connected sockets.

    Never raises: a socket that cannot be written within ``send_timeout_s``
    is closed and forgotten, and the event is unaffected.
    """

    name = "ws_fanout"
    patterns = ("*",)

    def __init__(self, send_timeout_s: float = 2.0):
        self._subs: list[_Subscription] = []
        self._send_timeout_s = send_timeout_s

    def add(self, ws: web.WebSocketResponse, patterns=("*",)) -> _Subscription:
        sub = _Subscription(ws, tuple(patterns))
        self._subs.append(sub)
        return sub

    def remove(self, sub: _Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    @property
    def connections(self) -> int:
        return len(self._subs)

    async def handle(self, event: Event, ctx: HandlerContext) -> None:
        if not self._subs:
            return
        text = json.dumps(event.to_dict())
        for sub in list(self._subs):
            if sub.ws.closed:
                self.remove(sub)
                continue
            if not matches(sub.patterns, event.type):
                continue
            try:
                await asyncio.wait_for(sub.ws.send_str(text), self._send_timeout_s)
            except Exception as e:
                log.info("dropping slow or dead websocket client: %s", e)
                self.remove(sub)
                await self._close(sub.ws, 1011)

    async def close_all(self, code: int = 1001) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            await self._close(sub.ws, code)

    @staticmethod
    async def _close(ws: web.WebSocketResponse, code: int) -> None:
        try:
            await ws.close(code=code, message=b"shutting down")
        except Exception:
            pass


SETTINGS = web.AppKey("settings", Settings)
BUS = web.AppKey("bus", EventBus)
STORE = web.AppKey("store", AsyncStore)
STATE = web.AppKey("state", HealthState)
FANOUT = web.AppKey("fanout", WebSocketFanout)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _authorized(request: web.Request, token: str | None, *, allow_query: bool = False) -> bool:
    if token is None:
        return True
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    if not supplied and allow_query:
        supplied = request.query.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


async def health(request: web.Request) -> web.Response:
    app = request.app
    state = app[STATE]
    return web.json_response({
        "status": "ok",
        "node_id": app[SETTINGS].node_id,
        "version": friday.__version__,
        "uptime_s": max(0.0, time.time() - state.started_at),
        "platform": state.platform.to_dict(),
        "queue": await app[STORE].queue_depths(),
        "supervisor_restarts": dict(state.restarts),
    })


async def telemetry(request: web.Request) -> web.Response:
    snapshot = await request.app[STORE].telemetry_latest(request.app[SETTINGS].node_id)
    return web.Response(status=204) if snapshot is None else web.json_response(snapshot)


async def nodes(request: web.Request) -> web.Response:
    rows = await request.app[STORE].heartbeats()
    return web.json_response([{"node_id": r.node_id, "last_seen": r.last_seen,
                               "status": r.status, "meta": r.meta} for r in rows])


async def post_events(request: web.Request) -> web.Response:
    app = request.app
    if not _authorized(request, app[SETTINGS].sentinel_token):
        return _error(401, "missing or invalid bearer token")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _error(413, f"body exceeds {MAX_BODY_BYTES} bytes")
    if len(body) > MAX_BODY_BYTES:
        return _error(413, f"body exceeds {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(body)
    except ValueError:
        return _error(400, "body is not valid JSON")
    items = data if isinstance(data, list) else [data]
    if not items:
        return _error(400, "no events in body")
    if len(items) > MAX_BATCH:
        return _error(400, f"at most {MAX_BATCH} events per request")

    now = time.time()
    events: list[Event] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return _error(400, f"event {index} is not an object")
        item = dict(item)
        item.setdefault("ts", now)
        try:
            events.append(Event.from_dict(item))
        except EventValidationError as e:
            return _error(400, f"event {index}: {e}")

    ids = [await app[BUS].publish(event) for event in events]
    return web.json_response({"ids": ids}, status=202)


async def websocket(request: web.Request) -> web.StreamResponse:
    app = request.app
    if not _authorized(request, app[SETTINGS].sentinel_token, allow_query=True):
        return _error(401, "missing or invalid token")
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    fanout = app[FANOUT]
    sub = fanout.add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                await ws.send_json({"error": "message is not JSON"})
                continue
            patterns = data.get("subscribe") if isinstance(data, dict) else None
            if (isinstance(patterns, list) and patterns
                    and all(isinstance(p, str) and p for p in patterns)):
                sub.patterns = tuple(patterns)
                await ws.send_json({"subscribed": list(sub.patterns)})
            else:
                await ws.send_json({"error": 'expected {"subscribe": ["pattern", ...]}'})
    finally:
        fanout.remove(sub)
    return ws


def create_app(settings: Settings, bus: EventBus, store: AsyncStore,
               state: HealthState) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    fanout = WebSocketFanout()
    bus.subscribe(fanout)
    app[SETTINGS] = settings
    app[BUS] = bus
    app[STORE] = store
    app[STATE] = state
    app[FANOUT] = fanout
    app.add_routes([
        web.get("/health", health),
        web.get("/telemetry", telemetry),
        web.get("/nodes", nodes),
        web.post("/events", post_events),
        web.get("/ws", websocket),
    ])
    return app


class ApiServer:
    def __init__(self, settings: Settings, bus: EventBus, store: AsyncStore, state: HealthState):
        self._settings = settings
        self._bus = bus
        self.app = create_app(settings, bus, store, state)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def fanout(self) -> WebSocketFanout:
        return self.app[FANOUT]

    async def start(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._settings.sentinel_bind_host, self._settings.sentinel_bind_port)
        await site.start()
        self._runner = runner
        addresses = runner.addresses
        self.port = addresses[0][1] if addresses else self._settings.sentinel_bind_port

    async def stop(self) -> None:
        self._bus.unsubscribe(self.fanout)
        await self.fanout.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_api.py -v`
Expected: all PASS.

---

### Task 14: `friday.sentinel.monitors`

**Files:**
- Create: `friday/sentinel/monitors.py`
- Test: `tests/sentinel/test_monitors.py`

**Interfaces:**
- Consumes: `collect` (Task 7), `Event`, `Heartbeat`, `detect`, `sd_notify` (Task 12), `HandlerContext`, `friday.__version__`
- Produces: `TelemetryMonitor(interval_s)`, `SelfHeartbeat(interval_s)`, `Housekeeping(interval_s=3600.0)` — each with `name: str` and `async run(ctx: HandlerContext) -> None` (loops forever; cancelled by the daemon).

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_monitors.py`:
```python
import asyncio
import logging
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

import friday
from friday.core.events import Heartbeat
from friday.core.storage import AsyncStore
from friday.sentinel import monitors
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext
from friday.sentinel.monitors import Housekeeping, SelfHeartbeat, TelemetryMonitor


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def rig(make_settings, tmp_path):
    settings = make_settings(FRIDAY_NODE_ID="sentinel-test", FRIDAY_RETENTION_DAYS="1")
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store)          # no dispatcher: tests inspect the queue directly
    ctx = HandlerContext(settings=settings, store=store, bus=bus, logger=logging.getLogger("test.mon"))
    yield SimpleNamespace(store=store, ctx=ctx)
    await store.aclose()


async def _run_until(monitor, ctx, predicate):
    task = asyncio.create_task(monitor.run(ctx))
    try:
        await until(predicate)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_telemetry_monitor_publishes_samples(rig):
    async def seen():
        return bool(await rig.store.list_events(type="telemetry.sample"))
    await _run_until(TelemetryMonitor(0.01), rig.ctx, seen)
    row = (await rig.store.list_events(type="telemetry.sample"))[0]
    assert row.event.source == "sentinel-test"
    assert "cpu_percent" in row.event.payload and "platform" in row.event.payload


async def test_self_heartbeat_publishes_and_pings_watchdog(rig, monkeypatch):
    pings = []
    monkeypatch.setattr(monitors, "sd_notify", lambda state: pings.append(state))

    async def seen():
        return bool(await rig.store.list_events(type="node.heartbeat"))
    await _run_until(SelfHeartbeat(0.01), rig.ctx, seen)
    row = (await rig.store.list_events(type="node.heartbeat"))[0]
    hb = Heartbeat.from_dict(row.event.payload)
    assert row.event.source == "sentinel-test"
    assert hb.status == "running" and hb.version == friday.__version__ and "/" in hb.platform
    assert "WATCHDOG=1" in pings


async def test_housekeeping_prunes_and_checkpoints(rig):
    await rig.store.telemetry_insert("n", {"old": True}, ts=1.0)
    await rig.store.telemetry_insert("n", {"new": True}, ts=time.time() + 10)

    async def pruned():
        return await rig.store.telemetry_count("n") == 1
    await _run_until(Housekeeping(0.01), rig.ctx, pruned)
    assert await rig.store.telemetry_latest("n") == {"new": True}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_monitors.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.monitors`.

- [ ] **Step 3: Implement**

`friday/sentinel/monitors.py`:
```python
"""Periodic tasks the daemon supervises. Each ``run`` loops until cancelled."""

from __future__ import annotations

import asyncio
import time
from functools import partial

import psutil

import friday
from friday.core.events import Event, Heartbeat
from friday.core.platform import detect
from friday.core.telemetry import collect
from friday.sentinel.handlers import HandlerContext
from friday.sentinel.sdnotify import sd_notify


class TelemetryMonitor:
    name = "telemetry"

    def __init__(self, interval_s: float):
        self.interval_s = interval_s

    async def run(self, ctx: HandlerContext) -> None:
        # cpu_percent measures since its previous call; the first call is always 0.
        psutil.cpu_percent(interval=None)
        loop = asyncio.get_running_loop()
        settings = ctx.settings
        while True:
            snapshot = await loop.run_in_executor(
                None, partial(collect, settings.node_id, settings.data_dir))
            await ctx.bus.publish(Event(type="telemetry.sample", source=settings.node_id,
                                        payload=snapshot.to_dict()))
            await asyncio.sleep(self.interval_s)


class SelfHeartbeat:
    name = "heartbeat"

    def __init__(self, interval_s: float):
        self.interval_s = interval_s

    async def run(self, ctx: HandlerContext) -> None:
        platform = detect().summary
        node_id = ctx.settings.node_id
        while True:
            hb = Heartbeat(status="running", version=friday.__version__, platform=platform)
            await ctx.bus.publish(Event(type="node.heartbeat", source=node_id, payload=hb.to_dict()))
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(self.interval_s)


class Housekeeping:
    name = "housekeeping"

    def __init__(self, interval_s: float = 3600.0):
        self.interval_s = interval_s

    async def run(self, ctx: HandlerContext) -> None:
        while True:
            await asyncio.sleep(self.interval_s)          # nothing to prune at boot
            cutoff = time.time() - ctx.settings.retention_days * 86400
            counts = await ctx.store.prune(cutoff)
            await ctx.store.checkpoint("PASSIVE")
            ctx.logger.info("housekeeping: pruned %d telemetry rows, %d events",
                            counts["telemetry"], counts["events"])
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_monitors.py -v`
Expected: all PASS.

---

### Task 15: `friday.sentinel.bridges`

**Files:**
- Modify: `friday/sentinel/bridges/__init__.py`
- Test: `tests/sentinel/test_bridges.py`

**Interfaces:**
- Produces: `class Bridge(Protocol)`: `name: str`, `async start(bus: EventBus, ctx: HandlerContext) -> None`, `async stop() -> None`; `load_bridges(settings: Settings) -> list[Bridge]`; test double `tests.sentinel.test_bridges.FakeBridge` (class attribute `instances: list`).

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_bridges.py`:
```python
import pytest

from friday.core.config import ConfigError
from friday.sentinel.bridges import load_bridges


class FakeBridge:
    name = "fake"
    instances: list = []

    def __init__(self):
        self.started_with = None
        self.stopped = False
        FakeBridge.instances.append(self)

    async def start(self, bus, ctx):
        self.started_with = (bus, ctx)

    async def stop(self):
        self.stopped = True


def test_no_bridges_by_default(make_settings):
    assert load_bridges(make_settings()) == []


def test_loads_dotted_class_paths(make_settings):
    FakeBridge.instances.clear()
    bridges = load_bridges(make_settings(FRIDAY_BRIDGES=f"{__name__}.FakeBridge, {__name__}.FakeBridge"))
    assert len(bridges) == 2 and all(isinstance(b, FakeBridge) for b in bridges)
    assert bridges[0] is not bridges[1]


@pytest.mark.parametrize("path", ["nodots", f"{__name__}.Missing", "no.such.module.Cls", f"{__name__}.pytest"])
def test_bad_paths_are_config_errors(make_settings, path):
    with pytest.raises(ConfigError) as excinfo:
        load_bridges(make_settings(FRIDAY_BRIDGES=path))
    assert "FRIDAY_BRIDGES" in str(excinfo.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_bridges.py -v`
Expected: `ImportError: cannot import name 'load_bridges'`.

- [ ] **Step 3: Implement**

`friday/sentinel/bridges/__init__.py`:
```python
"""Bridges: long-lived adapters between the bus and the outside world.

A bridge is started with the bus and may both publish inbound events
(an SMS arriving -> ``message.received``) and subscribe handlers for the
``command.*`` events it can act on (``command.sms.send``). The sentinel
knows nothing about what a bridge does; ``FRIDAY_BRIDGES`` names the
classes to load. None ship yet — this is the seam for Termux telephony,
WhatsApp, MQTT and cloud voice.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Protocol

from friday.core.config import ConfigError, Settings


class Bridge(Protocol):
    name: str

    async def start(self, bus: Any, ctx: Any) -> None: ...

    async def stop(self) -> None: ...


def load_bridges(settings: Settings) -> list[Bridge]:
    bridges: list[Bridge] = []
    for path in settings.bridges:
        module_name, _, attr = path.rpartition(".")
        if not module_name or not attr:
            raise ConfigError(f"FRIDAY_BRIDGES: {path!r} is not a dotted class path")
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"FRIDAY_BRIDGES: cannot load {path!r}: {e}") from e
        if not inspect.isclass(cls):
            raise ConfigError(f"FRIDAY_BRIDGES: {path!r} is not a class")
        bridges.append(cls())
    return bridges
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_bridges.py -v`
Expected: all PASS.

---

### Task 16: `friday.sentinel.daemon` and `__main__`

**Files:**
- Create: `friday/sentinel/daemon.py`, `friday/sentinel/__main__.py`
- Test: `tests/sentinel/test_daemon.py`

**Interfaces:**
- Consumes: everything from Tasks 2–15
- Produces:
  - `class Sentinel(settings)` with `ready: asyncio.Event`, `api_port: int | None`, `state: HealthState`, `request_shutdown(reason: str)` (thread-safe), `async run() -> int`, `RESTART_BACKOFF_BASE_S = 2.0`, `RESTART_BACKOFF_MAX_S = 30.0`, `SHUTDOWN_TIMEOUT_S = 10.0`, `BRIDGE_STOP_TIMEOUT_S = 3.0`
  - `friday.sentinel.__main__.main(argv=None) -> int`
  - `run()` raises `ConfigError` (after teardown) so `main` can print one line; any other boot exception is logged with traceback and returns 1.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_daemon.py`:
```python
import asyncio
import logging
import os
import signal
import threading
import time

import aiohttp
import pytest

from friday.core.config import ConfigError, load_settings
from friday.core.events import Event
from friday.core.storage import Store
from friday.sentinel.daemon import Sentinel
from tests.sentinel.test_bridges import FakeBridge


def _settings(tmp_path, **extra):
    env = {
        "FRIDAY_DATA_DIR": str(tmp_path / "data"),
        "FRIDAY_NODE_ID": "sentinel-test",
        "FRIDAY_SENTINEL_BIND": "127.0.0.1:0",
        "FRIDAY_TELEMETRY_INTERVAL": "0.05",
        "FRIDAY_HEARTBEAT_INTERVAL": "0.05",
        "FRIDAY_LOG_LEVEL": "DEBUG",
        **extra,
    }
    return load_settings(env=env, env_file=tmp_path / "absent.env")


async def _start(sentinel):
    task = asyncio.create_task(sentinel.run())
    await asyncio.wait_for(sentinel.ready.wait(), 10)
    return task


async def test_boot_health_and_sigterm_shutdown(tmp_path):
    settings = _settings(tmp_path)
    sentinel = Sentinel(settings)
    task = await _start(sentinel)
    assert sentinel.api_port

    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{sentinel.api_port}/health") as resp:
            assert resp.status == 200
            assert (await resp.json())["node_id"] == "sentinel-test"

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 10) == 0

    db = settings.data_dir / "sentinel.db"
    wal = settings.data_dir / "sentinel.db-wal"
    assert db.exists()
    assert not wal.exists() or wal.stat().st_size == 0
    store = Store.open(db)
    try:
        types = {r.event.type for r in store.list_events(limit=1000)}
        assert {"sentinel.started", "sentinel.stopping", "node.heartbeat", "telemetry.sample"} <= types
        assert store.queue_depths()["processing"] == 0
        assert store.heartbeats()[0].node_id == "sentinel-test"
    finally:
        store.close()


async def test_request_shutdown_is_thread_safe(tmp_path):
    sentinel = Sentinel(_settings(tmp_path))
    task = await _start(sentinel)
    threading.Thread(target=sentinel.request_shutdown, args=("from thread",)).start()
    assert await asyncio.wait_for(task, 10) == 0


async def test_orphaned_events_are_requeued_and_processed(tmp_path):
    settings = _settings(tmp_path)
    db = settings.data_dir / "sentinel.db"
    seed = Store.open(db)
    seed.enqueue(Event(type="x.y", source="s", id="orphan"))
    seed.claim(1, now=time.time())
    assert seed.queue_depths()["processing"] == 1
    seed.close()

    sentinel = Sentinel(settings)
    task = await _start(sentinel)
    reader = Store.open(db)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if reader.list_events(type="x.y")[0].status == "done":
                break
            await asyncio.sleep(0.02)
        assert reader.list_events(type="x.y")[0].status == "done"
    finally:
        reader.close()
    sentinel.request_shutdown("test done")
    assert await asyncio.wait_for(task, 10) == 0


async def test_bridge_lifecycle(tmp_path):
    FakeBridge.instances.clear()
    sentinel = Sentinel(_settings(tmp_path, FRIDAY_BRIDGES="tests.sentinel.test_bridges.FakeBridge"))
    task = await _start(sentinel)
    bridge = FakeBridge.instances[0]
    assert bridge.started_with is not None
    bus, ctx = bridge.started_with
    assert hasattr(bus, "publish") and ctx.settings.node_id == "sentinel-test"
    sentinel.request_shutdown("test done")
    assert await asyncio.wait_for(task, 10) == 0
    assert bridge.stopped is True


async def test_bad_bridge_config_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        await Sentinel(_settings(tmp_path, FRIDAY_BRIDGES="no.such.Bridge")).run()


async def test_supervisor_restarts_crashed_task(tmp_path):
    sentinel = Sentinel(_settings(tmp_path))
    sentinel.RESTART_BACKOFF_BASE_S = 0.001
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")

    await asyncio.wait_for(sentinel._supervise("flaky", flaky, logging.getLogger("test")), 5)
    assert len(calls) == 3
    assert sentinel.state.restarts["flaky"] == 2


def test_main_reports_config_error(monkeypatch, capsys, tmp_path):
    from friday.sentinel.__main__ import main

    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    monkeypatch.setenv("FRIDAY_BRIDGES", "no.such.Bridge")
    assert main() == 1
    assert "FRIDAY_BRIDGES" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_daemon.py -v`
Expected: `ModuleNotFoundError: friday.sentinel.daemon`.

- [ ] **Step 3: Implement**

`friday/sentinel/daemon.py`:
```python
"""The sentinel process: boot, supervise, shut down in order.

Runs the same on macOS (foreground or launchd) and Linux (systemd,
``Type=notify``). Signals are installed before anything else is started so
a SIGTERM during a slow boot still exits in order; every long-running task
is supervised so one crash never takes the daemon down silently.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

import friday
from friday.core.config import ConfigError, Settings
from friday.core.events import Event
from friday.core.logsetup import configure_logging
from friday.core.platform import detect
from friday.core.storage import AsyncStore
from friday.sentinel.api import ApiServer, HealthState
from friday.sentinel.bridges import Bridge, load_bridges
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import (HandlerContext, HeartbeatHandler, LogHandler,
                                      TelemetryHandler)
from friday.sentinel.monitors import Housekeeping, SelfHeartbeat, TelemetryMonitor
from friday.sentinel.sdnotify import sd_notify


class Sentinel:
    RESTART_BACKOFF_BASE_S = 2.0
    RESTART_BACKOFF_MAX_S = 30.0
    SHUTDOWN_TIMEOUT_S = 10.0
    BRIDGE_STOP_TIMEOUT_S = 3.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ready = asyncio.Event()
        self.api_port: int | None = None
        self.state = HealthState(started_at=time.time(), platform=detect())
        self._shutdown = asyncio.Event()
        self._reason = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log = logging.getLogger("friday.sentinel")

    # ------------------------------------------------------------ control

    def request_shutdown(self, reason: str = "") -> None:
        """Begin an orderly shutdown. Safe to call from any thread or a signal handler."""
        if self._shutdown.is_set():
            return
        self._reason = reason or self._reason
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._shutdown.set)
        else:
            self._shutdown.set()

    # --------------------------------------------------------------- run

    async def run(self) -> int:
        s = self.settings
        configure_logging(s.log_level)
        log = self._log
        self._loop = asyncio.get_running_loop()
        self.state.started_at = time.time()
        info = self.state.platform
        log.info("FRIDAY sentinel %s starting: %s (%s, %s) node=%s data=%s",
                 friday.__version__, info.summary, info.hostname,
                 info.distro or "no distro info", s.node_id, s.data_dir)
        self._install_signal_handlers()

        store: AsyncStore | None = None
        bus: EventBus | None = None
        server: ApiServer | None = None
        bridges: list[Bridge] = []
        monitor_tasks: list[asyncio.Task] = []
        dispatcher_task: asyncio.Task | None = None
        config_error: ConfigError | None = None
        exit_code = 0

        try:
            store = await AsyncStore.open(s.data_dir / "sentinel.db", synchronous=s.db_synchronous)
            requeued = await store.requeue_stale(0.0, time.time())
            if requeued:
                log.warning("requeued %d event(s) orphaned by a previous run", requeued)

            bus = EventBus(store)
            ctx = HandlerContext(settings=s, store=store, bus=bus,
                                 logger=logging.getLogger("friday.sentinel.events"))
            for handler in (HeartbeatHandler(), TelemetryHandler(), LogHandler()):
                bus.subscribe(handler)

            for bridge in load_bridges(s):
                try:
                    await bridge.start(bus, ctx)
                    bridges.append(bridge)
                    log.info("bridge %s started", bridge.name)
                except Exception:
                    log.exception("bridge %s failed to start; skipping it",
                                  getattr(bridge, "name", bridge))

            server = ApiServer(s, bus, store, self.state)
            await server.start()
            self.api_port = server.port
            log.info("API listening on http://%s:%s", s.sentinel_bind_host, server.port)

            for monitor in (TelemetryMonitor(s.telemetry_interval_s),
                            SelfHeartbeat(s.heartbeat_interval_s),
                            Housekeeping()):
                monitor_tasks.append(asyncio.create_task(
                    self._supervise(monitor.name, lambda m=monitor: m.run(ctx), log),
                    name=f"monitor:{monitor.name}"))
            dispatcher_task = asyncio.create_task(
                self._supervise("dispatcher", lambda: bus.run_dispatcher(ctx), log),
                name="dispatcher")

            await bus.publish(Event(type="sentinel.started", source=s.node_id,
                                    payload={"version": friday.__version__,
                                             "platform": info.to_dict()}))
            sd_notify("READY=1")
            self.ready.set()
            log.info("ready")

            await self._shutdown.wait()
            log.info("shutting down (%s)", self._reason or "no reason given")
        except ConfigError as e:
            config_error = e
            exit_code = 1
        except Exception:
            log.exception("fatal error during boot")
            exit_code = 1
        finally:
            await self._teardown(store, bus, server, bridges, monitor_tasks, dispatcher_task)
            self._remove_signal_handlers()
            log.info("stopped")

        if config_error is not None:
            raise config_error
        return exit_code

    # ---------------------------------------------------------- teardown

    async def _teardown(self, store, bus, server, bridges, monitor_tasks, dispatcher_task) -> None:
        log = self._log

        async def ordered() -> None:
            sd_notify("STOPPING=1")
            if bus is not None:
                try:
                    await bus.publish(Event(type="sentinel.stopping", source=self.settings.node_id,
                                            payload={"reason": self._reason}))
                except Exception as e:
                    log.warning("could not record sentinel.stopping: %s", e)
            if server is not None:
                await server.stop()
            for task in monitor_tasks:
                task.cancel()
            if monitor_tasks:
                await asyncio.gather(*monitor_tasks, return_exceptions=True)
            for bridge in bridges:
                try:
                    await asyncio.wait_for(bridge.stop(), self.BRIDGE_STOP_TIMEOUT_S)
                except Exception as e:
                    log.warning("bridge %s did not stop cleanly: %s", bridge.name, e)
            if bus is not None:
                await bus.drain()
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                await asyncio.gather(dispatcher_task, return_exceptions=True)

        try:
            await asyncio.wait_for(ordered(), self.SHUTDOWN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("shutdown exceeded %.0fs; cancelling what is left", self.SHUTDOWN_TIMEOUT_S)
            for task in [*monitor_tasks, dispatcher_task]:
                if task is not None:
                    task.cancel()
        if store is not None:
            try:
                await store.aclose()
            except Exception:
                log.exception("store close failed")

    # --------------------------------------------------------- supervise

    async def _supervise(self, name: str, factory, log: logging.Logger) -> None:
        """Run ``factory()`` to completion, restarting with backoff if it raises."""
        failures = 0
        while not self._shutdown.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                self.state.restarts[name] = self.state.restarts.get(name, 0) + 1
                delay = min(self.RESTART_BACKOFF_MAX_S,
                            self.RESTART_BACKOFF_BASE_S * (2 ** (failures - 1)))
                log.exception("%s crashed; restarting in %.3gs", name, delay)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    # ----------------------------------------------------------- signals

    def _install_signal_handlers(self) -> None:
        loop = self._loop
        assert loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)
            except (NotImplementedError, RuntimeError, ValueError, AttributeError) as e:
                self._log.debug("cannot install handler for %s: %s", sig.name, e)
        hup = getattr(signal, "SIGHUP", None)
        if hup is not None:
            try:
                loop.add_signal_handler(
                    hup, lambda: self._log.info("SIGHUP received; reload not supported, ignoring"))
            except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                pass

    def _remove_signal_handlers(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                pass
```

`friday/sentinel/__main__.py`:
```python
"""``python -m friday.sentinel`` / ``friday-sentinel``."""

from __future__ import annotations

import asyncio
import sqlite3
import sys

from friday.core.config import ConfigError, load_settings
from friday.sentinel.daemon import Sentinel


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
        return asyncio.run(Sentinel(settings).run())
    except ConfigError as e:
        print(f"friday-sentinel: configuration error: {e}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as e:
        print(f"friday-sentinel: cannot start: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_daemon.py -v`
Expected: all PASS. Then a manual foreground run:

```bash
FRIDAY_SENTINEL_BIND=127.0.0.1:8770 .venv/bin/python -m friday.sentinel &
sleep 2; curl -s 127.0.0.1:8770/health; echo; curl -s 127.0.0.1:8770/nodes; echo
kill -TERM %1; wait
```
Expected: `/health` returns JSON with `"status":"ok"`; `/nodes` lists `sentinel-test`'s hostname; the process logs `shutting down (SIGTERM)` then `stopped` and exits 0.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all PASS.

---

### Task 17: Boundary test — no macOS-only imports in `core`/`sentinel`

**Files:**
- Test: `tests/test_boundaries.py`

- [ ] **Step 1: Write the test**

`tests/test_boundaries.py`:
```python
"""friday.core and friday.sentinel must import on a headless Linux box with
only the base dependencies. A fresh interpreter imports every submodule with
the macOS/desktop-only modules poisoned; any leak fails here, not on the Pi."""

import os
import pkgutil
import subprocess
import sys

import friday.core
import friday.sentinel

POISON = ("Quartz", "pyaudio", "cv2", "webview", "EventKit", "Foundation", "AppKit", "objc",
          "pyautogui", "termios", "tty", "PIL", "numpy")

SITECUSTOMIZE = """
import sys
POISON = {poison!r}

class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in POISON:
            raise ImportError(f"{{name}} is not allowed in friday.core / friday.sentinel")
        return None

sys.meta_path.insert(0, _Blocker())
"""

SCRIPT = """
import importlib, sys
failed = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except ImportError as e:
        failed.append(f"{{name}}: {{e}}")
print("\\n".join(failed))
sys.exit(1 if failed else 0)
"""


def _modules(package) -> list[str]:
    names = [package.__name__]
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        names.append(info.name)
    return names


def test_core_and_sentinel_never_import_platform_modules(tmp_path):
    modules = sorted(set(_modules(friday.core) + _modules(friday.sentinel)))
    assert "friday.core.storage" in modules and "friday.sentinel.daemon" in modules
    (tmp_path / "sitecustomize.py").write_text(SITECUSTOMIZE.format(poison=POISON))
    env = {**os.environ,
           "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, "-c", SCRIPT.format(modules=modules)],
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"leaked platform imports:\n{result.stdout}\n{result.stderr}"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_boundaries.py -v`
Expected: PASS. If it fails, the output names the module and the poisoned import; fix the leak in the module (never widen `POISON`).

- [ ] **Step 3: Prove the guard works**

Temporarily add `import numpy` to the top of `friday/core/platform.py`, re-run: Expected FAIL naming `friday.core.platform: numpy is not allowed…`. Remove the line, re-run: PASS.

---

### Task 18: `deploy/` — systemd, launchd, README

**Files:**
- Create: `deploy/systemd/friday-sentinel.service`, `deploy/launchd/com.friday.sentinel.plist`, `deploy/README.md`
- Move: `setup_remote.sh` → `deploy/setup_remote.sh` (`git mv`)

- [ ] **Step 1: Write the unit files**

`deploy/systemd/friday-sentinel.service`:
```ini
# FRIDAY sentinel — systemd unit (Fedora, Raspberry Pi OS, any systemd distro).
# Replace __USER__ and __REPO__ (see deploy/README.md), then:
#   sudo cp deploy/systemd/friday-sentinel.service /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now friday-sentinel

[Unit]
Description=FRIDAY sentinel (24/7 event bus, telemetry, bridges)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=__USER__
WorkingDirectory=__REPO__
EnvironmentFile=-__REPO__/.env
ExecStart=__REPO__/.venv/bin/python -m friday.sentinel
Restart=always
RestartSec=5
# The daemon pings WATCHDOG=1 every FRIDAY_HEARTBEAT_INTERVAL (30 s default);
# three missed beats and systemd restarts it.
WatchdogSec=90
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=__REPO__/data

[Install]
WantedBy=multi-user.target
```

`deploy/launchd/com.friday.sentinel.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  FRIDAY sentinel — launchd agent for development on macOS.
  Replace __REPO__, then:
    cp deploy/launchd/com.friday.sentinel.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.friday.sentinel.plist
  Stop with: launchctl bootout gui/$(id -u)/com.friday.sentinel
-->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.friday.sentinel</string>
  <key>ProgramArguments</key>
  <array>
    <string>__REPO__/.venv/bin/python</string>
    <string>-m</string>
    <string>friday.sentinel</string>
  </array>
  <key>WorkingDirectory</key>
  <string>__REPO__</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>__REPO__/data/logs/sentinel.log</string>
  <key>StandardErrorPath</key>
  <string>__REPO__/data/logs/sentinel.log</string>
</dict>
</plist>
```

`deploy/README.md`:
````markdown
# Deploying the FRIDAY sentinel

The sentinel is `python -m friday.sentinel`. It reads `.env` from the repo
root, keeps all state under `data/` (or `FRIDAY_DATA_DIR`), and listens on
`FRIDAY_SENTINEL_BIND` (default `127.0.0.1:8770`).

## Any node: install

```bash
git clone <repo> && cd friday-ai-assistant
python3 -m venv .venv
.venv/bin/pip install -e ".[sentinel]"        # headless server / Pi
cp .env.template .env                          # set FRIDAY_SENTINEL_TOKEN at least
.venv/bin/python -m friday.sentinel            # foreground smoke run
curl -s 127.0.0.1:8770/health
```

## Linux (Fedora, Raspberry Pi OS): systemd

```bash
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$PWD|g" \
    deploy/systemd/friday-sentinel.service | sudo tee /etc/systemd/system/friday-sentinel.service
sudo systemctl daemon-reload
sudo systemctl enable --now friday-sentinel
journalctl -u friday-sentinel -f
```

`Type=notify` + `WatchdogSec=90`: the daemon reports READY and pings the
watchdog on every heartbeat, so a hung process is restarted automatically.

## macOS (development): launchd

```bash
mkdir -p data/logs ~/Library/LaunchAgents
sed "s|__REPO__|$PWD|g" deploy/launchd/com.friday.sentinel.plist > ~/Library/LaunchAgents/com.friday.sentinel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.friday.sentinel.plist
tail -f data/logs/sentinel.log
```

## Remote access (Tailscale Serve)

The sentinel binds to localhost. To reach it from other nodes on your
tailnet, on the sentinel host:

```bash
tailscale serve --bg --https=443 --set-path=/sentinel http://127.0.0.1:8770
```

Then point the desktop at it in its `.env`:

```ini
FRIDAY_SENTINEL_URL=https://<host>.<tailnet>.ts.net/sentinel
FRIDAY_SENTINEL_TOKEN=<same token as the sentinel>
```

`setup_remote.sh` in this directory is the existing script for exposing the
**desktop hub** the same way; it is unchanged.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | status, queue depths, platform, supervisor restarts |
| `GET /telemetry` | none | latest telemetry snapshot (204 if none yet) |
| `GET /nodes` | none | last heartbeat per node |
| `POST /events` | bearer | one event or a list (≤100, ≤256 KB): `{"type":"a.b","source":"node","payload":{}}` → `202 {"ids":[...]}` |
| `GET /ws` | bearer (header or `?token=`) | stream events; send `{"subscribe":["node.*"]}` to filter |
````

- [ ] **Step 2: Move the remote-access script**

Run: `git mv setup_remote.sh deploy/setup_remote.sh`

- [ ] **Step 3: Validate the plist parses**

Run: `plutil -lint deploy/launchd/com.friday.sentinel.plist`
Expected: `deploy/launchd/com.friday.sentinel.plist: OK`.

---

### Task 19: Desktop relocation

**Files:**
- Move (`git mv`): `friday_hub.py→friday/desktop/hub.py`, `app_desktop.py→friday/desktop/app.py`, `friday_agents.py→friday/desktop/agents.py`, `widget_generator_agent.py→friday/desktop/widget_generator.py`, `services/asset_generator.py→friday/desktop/asset_generator.py`, `sentry_{vision,action,exec,recognition,scene,personal,web}.py→friday/desktop/`, `web_gui/→friday/desktop/web_gui/`, `face_detection_yunet.onnx` + `face_recognition_sface.onnx → friday/desktop/models/`
- Move (plain `mv`, git-ignored): `friday_memory.json friday_profiles.json friday_scenes.json friday_assets.json friday_history.jsonl generated_assets/ .webview/ → data/`
- Modify: `friday/desktop/hub.py`, `friday/desktop/app.py`, `friday/desktop/agents.py`, `friday/desktop/widget_generator.py`, `friday/desktop/asset_generator.py`, `friday/desktop/sentry_action.py`, `friday/desktop/sentry_scene.py`, `friday/desktop/sentry_recognition.py`
- Create: `friday/desktop/__main__.py`
- Test: `tests/desktop/test_imports.py`

**Interfaces:**
- Consumes: `get_settings` (Task 2), `resolve`, `gemini_client` (Task 9)
- Produces: `friday.desktop.hub.settings: Settings`, `friday.desktop.hub.current_system_status: str`, `friday.desktop.app.main() -> int`; `python -m friday.desktop` boots the app; `python -m friday.desktop.hub` runs the headless engine.

- [ ] **Step 1: Write the failing test**

`tests/desktop/test_imports.py`:
```python
"""Import smoke for the macOS node. Skipped wherever the desktop extra is absent."""

import importlib

import pytest

pytest.importorskip("Quartz", reason="desktop extra not installed")

MODULES = [
    "friday.desktop.sentry_web", "friday.desktop.sentry_scene", "friday.desktop.sentry_exec",
    "friday.desktop.sentry_vision", "friday.desktop.sentry_action", "friday.desktop.sentry_recognition",
    "friday.desktop.sentry_personal", "friday.desktop.asset_generator", "friday.desktop.agents",
    "friday.desktop.widget_generator", "friday.desktop.hub", "friday.desktop.app",
]


@pytest.fixture(scope="module", autouse=True)
def desktop_data_dir(tmp_path_factory):
    """One data dir for the whole module: the desktop modules bake paths in at import."""
    from friday.core import config

    data_dir = tmp_path_factory.mktemp("desktop-data").resolve()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FRIDAY_DATA_DIR", str(data_dir))
        config.get_settings.cache_clear()
        yield data_dir
    config.get_settings.cache_clear()


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_hub_paths_live_under_data_dir(desktop_data_dir):
    from friday.core.config import REPO_ROOT
    from friday.desktop import hub, sentry_recognition, sentry_scene

    data = str(desktop_data_dir)
    assert data != str(REPO_ROOT / "data")
    assert hub.MEMORY_FILE.startswith(data)
    assert hub.HISTORY_LOG_FILE.startswith(data)
    assert hub.ASSETS_DIR.startswith(data)
    assert sentry_scene.SCENES_FILE.startswith(data)
    assert sentry_recognition.PROFILES_FILE.startswith(data)
    assert sentry_recognition.YUNET_MODEL_PATH.endswith("friday/desktop/models/face_detection_yunet.onnx")


def test_hub_models_come_from_routing():
    from friday.core.llm import resolve
    from friday.desktop import agents, hub, widget_generator

    assert hub.MODEL_ID == resolve(hub.settings, "live").model
    assert agents.OS_AGENT_MODEL == resolve(hub.settings, "agent_os").model
    assert agents.TIERS["os"]["model"] == agents.OS_AGENT_MODEL
    assert widget_generator.WIDGET_MODEL == resolve(hub.settings, "widget").model
```

- [ ] **Step 2: Move the files**

```bash
mkdir -p friday/desktop/models data
git mv friday_hub.py friday/desktop/hub.py
git mv app_desktop.py friday/desktop/app.py
git mv friday_agents.py friday/desktop/agents.py
git mv widget_generator_agent.py friday/desktop/widget_generator.py
git mv services/asset_generator.py friday/desktop/asset_generator.py
for m in vision action exec recognition scene personal web; do git mv "sentry_$m.py" "friday/desktop/sentry_$m.py"; done
git mv web_gui friday/desktop/web_gui
git mv face_detection_yunet.onnx face_recognition_sface.onnx friday/desktop/models/
git rm -q services/__init__.py && rmdir services
for f in friday_memory.json friday_profiles.json friday_scenes.json friday_assets.json friday_history.jsonl generated_assets .webview; do [ -e "$f" ] && mv "$f" data/; done
ls friday/desktop data
```

- [ ] **Step 3: Fix cross-imports between relocated modules**

Run: `grep -nE '^(import|from) (sentry_|friday_|widget_generator|services|friday_hub)' friday/desktop/*.py`
Expected hits (exact lines to change):
- `friday/desktop/hub.py:33-42` — the ten `import sentry_*` / `import friday_agents` / `import widget_generator_agent` / `import services.asset_generator` lines
- `friday/desktop/sentry_action.py:20` — `import sentry_vision`
- `friday/desktop/app.py:18` — `import friday_hub`

Replace hub.py lines 33–42 with:
```python
# Capability submodules
from friday.desktop import (agents, asset_generator, sentry_action, sentry_exec,
                            sentry_personal, sentry_recognition, sentry_scene,
                            sentry_vision, sentry_web, widget_generator)
```
Then rename the module-qualified call sites in hub.py:
```bash
sed -i '' -E 's/friday_agents\./agents./g; s/widget_generator_agent\./widget_generator./g; s/services\.asset_generator\./asset_generator./g' friday/desktop/hub.py
grep -nE 'friday_agents|widget_generator_agent|services\.' friday/desktop/hub.py
```
Expected: no output from the final grep.

`sentry_action.py:20`: `import sentry_vision` → `from friday.desktop import sentry_vision`.

`app.py:18`: `import friday_hub` → `from friday.desktop import hub as friday_hub`.

- [ ] **Step 4: Wire settings, paths and model routing in `hub.py`**

Replace `from dotenv import load_dotenv` (line 28) with:
```python
import friday
from friday.core.config import get_settings
from friday.core.llm import gemini_client, resolve
```

Replace `load_dotenv()` (line 44) with:
```python
settings = get_settings()
```

Replace lines 53–55:
```python
BASE_DIR = os.path.dirname(os.path.abspath(__file__))       # this package: web_gui/ lives here
DATA_DIR = str(settings.data_dir)                            # all runtime state
HISTORY_LOG_FILE = os.path.join(DATA_DIR, "friday_history.jsonl")
MEMORY_FILE = os.path.join(DATA_DIR, "friday_memory.json")
```

Replace line 58 (`MODEL_ID = os.getenv(...)`) with:
```python
# Model selection is routed per role from the environment (FRIDAY_LLM_LIVE,
# or the legacy GEMINI_MODEL alias); see friday.core.llm.
MODEL_ID = resolve(settings, "live").model
```
Replace line 62 (`LIVE_VOICE = os.getenv("FRIDAY_VOICE", "Aoede")`) with `LIVE_VOICE = settings.friday_voice`.

In `set_system_status` (line ~210) add a module global so the heartbeat can report it:
```python
current_system_status = "Booting"


def set_system_status(status_str: str):
    global current_system_status
    current_system_status = status_str
    print(f"[FRIDAY Status] {status_str}")
    broadcast_event({"type": "status", "status": status_str})
```

Replace lines ~2028–2029:
```python
ASSETS_DIR = os.path.join(DATA_DIR, "generated_assets")
ASSETS_INDEX_FILE = os.path.join(DATA_DIR, "friday_assets.json")
```

In `run_friday` replace
```python
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        set_system_status("ERROR")
        raise StartupError("GEMINI_API_KEY environment variable not set.")

    set_system_status("Initializing Client")
    client = genai.Client(api_key=api_key)
```
with
```python
    if not settings.gemini_api_key:
        set_system_status("ERROR")
        raise StartupError("GEMINI_API_KEY environment variable not set.")

    set_system_status("Initializing Client")
    client = gemini_client(settings)
```

Run: `grep -nE 'os\.getenv|load_dotenv|BASE_DIR' friday/desktop/hub.py`
Expected: only the `BASE_DIR =` definition line and the `os.environ["OPENCV_LOG_LEVEL"]` line remain; `gui_dir` at ~line 2107 still resolves `web_gui` next to the module, which is now correct.

- [ ] **Step 5: Route the agent and widget models, and the Tripo key**

`friday/desktop/agents.py` — replace
```python
OS_AGENT_MODEL = "gemini-3.8-flash"
SVE_AGENT_MODEL = "gemini-3.8-flash"
```
with
```python
from friday.core.config import get_settings
from friday.core.llm import resolve

# Routed per role from the environment (FRIDAY_LLM_AGENT_OS / _AGENT_SPATIAL).
OS_AGENT_MODEL = resolve(get_settings(), "agent_os").model
SVE_AGENT_MODEL = resolve(get_settings(), "agent_spatial").model
```
and update the module docstring's tier table to say the models come from `FRIDAY_LLM_AGENT_OS` / `FRIDAY_LLM_AGENT_SPATIAL` (default `gemini-3.8-flash`).

`friday/desktop/widget_generator.py` — replace `WIDGET_MODEL = "gemini-3.7-flash"` with:
```python
from friday.core.config import get_settings
from friday.core.llm import resolve

WIDGET_MODEL = resolve(get_settings(), "widget").model      # FRIDAY_LLM_WIDGET
```

`friday/desktop/asset_generator.py` — replace the `import os` line, the comment block above `TRIPO_API_KEY = None`, and `_api_key()` with:
```python
from friday.core.config import get_settings

# Module-level value is an override hook for tests; otherwise the key comes
# from Settings (TRIPO_API_KEY in .env or the environment).
TRIPO_API_KEY = None
BASE_URL = "https://api.tripo3d.ai/v2/openapi"


def _api_key() -> str:
    return TRIPO_API_KEY or get_settings().tripo_api_key or ""
```
(keep `import asyncio` and `import httpx`; drop `import os` only if nothing else in the file uses it — check with `grep -n 'os\.' friday/desktop/asset_generator.py`).

- [ ] **Step 6: Point scene, profile and model paths at the right places**

`friday/desktop/sentry_scene.py:25`:
```python
from friday.core.config import get_settings

SCENES_FILE = str(get_settings().data_dir / "friday_scenes.json")
```

`friday/desktop/sentry_recognition.py:22-24`:
```python
from friday.core.config import get_settings

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
PROFILES_FILE = str(get_settings().data_dir / "friday_profiles.json")
YUNET_MODEL_PATH = os.path.join(_MODELS_DIR, "face_detection_yunet.onnx")
SFACE_MODEL_PATH = os.path.join(_MODELS_DIR, "face_recognition_sface.onnx")
```

`friday/desktop/app.py` — replace the `WEBVIEW_STORAGE = ...` assignment (and its comment stays) with:
```python
WEBVIEW_STORAGE = str(friday_hub.settings.data_dir / ".webview")
```

- [ ] **Step 7: Entry point**

`friday/desktop/__main__.py`:
```python
"""``python -m friday.desktop`` / ``friday-desktop``: the macOS window shell."""

import sys

from friday.desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Verify**

Run: `.venv/bin/pytest tests/desktop -v`
Expected: every module imports; both path/model tests PASS.

Run: `.venv/bin/pytest -q`
Expected: all PASS.

Run (boot smoke, needs the real `.env`): `timeout 25 .venv/bin/python -m friday.desktop.hub; echo "exit=$?"`
Expected: logs reach `Listening` (or a Gemini connection attempt) and the GUI server line `http://127.0.0.1:8766`; exits on the timeout's SIGTERM with the graceful `Project FRIDAY engine terminated. Goodbye.` line. If `timeout` is missing on macOS, run it in the background and `kill -TERM` it after a few seconds.

---

### Task 20: Desktop → sentinel heartbeat

**Files:**
- Create: `friday/desktop/sentinel_client.py`
- Modify: `friday/desktop/hub.py` (imports, `sentinel_heartbeat_task`, wiring in `run_friday`)
- Test: `tests/desktop/test_sentinel_client.py`

**Interfaces:**
- Consumes: `Event`, `Heartbeat`, `detect`, `settings`, `current_system_status`, `shutdown_event`, `sleep_unless_shutdown`, `remote_ws_clients` (hub)
- Produces: `class SentinelClient(url: str, token: str | None, node_id: str, *, timeout_s: float = 5.0)` with `async post(event: Event) -> bool` (never raises), `async aclose()`; hub coroutine `sentinel_heartbeat_task()`.

- [ ] **Step 1: Write the failing tests**

`tests/desktop/test_sentinel_client.py`:
```python
import logging

from aiohttp import web

from friday.core.events import Event
from friday.desktop.sentinel_client import SentinelClient


def _app(status=202, received=None):
    async def events(request):
        if received is not None:
            received.append((dict(request.headers), await request.json()))
        return web.json_response({"ids": ["x"]}, status=status)

    app = web.Application()
    app.add_routes([web.post("/sentinel/events", events)])
    return app


async def test_post_success_sends_bearer_and_body(aiohttp_server):
    received = []
    server = await aiohttp_server(_app(received=received))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel/", "tok", "mac")
    try:
        ok = await client.post(Event(type="node.heartbeat", source="mac", payload={"status": "x"}))
    finally:
        await client.aclose()
    assert ok is True
    headers, body = received[0]
    assert headers["Authorization"] == "Bearer tok"
    assert body["type"] == "node.heartbeat" and body["source"] == "mac"


async def test_post_without_token_sends_no_header(aiohttp_server):
    received = []
    server = await aiohttp_server(_app(received=received))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", None, "mac")
    try:
        assert await client.post(Event(type="a.b", source="mac")) is True
    finally:
        await client.aclose()
    assert "Authorization" not in received[0][0]


async def test_non_202_is_false(aiohttp_server):
    server = await aiohttp_server(_app(status=500))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "t", "mac")
    try:
        assert await client.post(Event(type="a.b", source="mac")) is False
    finally:
        await client.aclose()


async def test_unreachable_is_false_and_warns_once(caplog):
    client = SentinelClient("http://127.0.0.1:9/sentinel", "t", "mac", timeout_s=0.5)
    try:
        with caplog.at_level(logging.DEBUG, logger="friday.desktop.sentinel_client"):
            assert await client.post(Event(type="a.b", source="mac")) is False
            assert await client.post(Event(type="a.b", source="mac")) is False
    finally:
        await client.aclose()
    levels = [r.levelno for r in caplog.records if "sentinel" in r.getMessage()]
    assert levels[0] == logging.WARNING
    assert all(level == logging.DEBUG for level in levels[1:])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/desktop/test_sentinel_client.py -v`
Expected: `ModuleNotFoundError: friday.desktop.sentinel_client`.

- [ ] **Step 3: Implement the client**

`friday/desktop/sentinel_client.py`:
```python
"""Fire-and-forget event poster from the desktop node to the sentinel.

``post`` never raises and never blocks the voice path for longer than the
request timeout: a sentinel that is down costs one WARNING line, then DEBUG
lines until it is back.
"""

from __future__ import annotations

import logging

import aiohttp

from friday.core.events import Event

log = logging.getLogger(__name__)


class SentinelClient:
    def __init__(self, url: str, token: str | None, node_id: str, *, timeout_s: float = 5.0):
        self._events_url = url.rstrip("/") + "/events"
        self._token = token
        self.node_id = node_id
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._failing = False

    async def post(self, event: Event) -> bool:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            async with self._session.post(self._events_url, json=event.to_dict(),
                                          headers=headers) as resp:
                if resp.status != 202:
                    raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            (log.debug if self._failing else log.warning)(
                "sentinel post to %s failed: %s", self._events_url, e)
            self._failing = True
            return False
        if self._failing:
            log.info("sentinel reachable again at %s", self._events_url)
            self._failing = False
        return True

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
```

- [ ] **Step 4: Wire the heartbeat into the hub**

In `friday/desktop/hub.py`, extend the core imports added in Task 19:
```python
import friday
from friday.core.config import get_settings
from friday.core.events import Event, Heartbeat
from friday.core.llm import gemini_client, resolve
from friday.core.platform import detect
from friday.desktop.sentinel_client import SentinelClient
```

Add after `telemetry_task()` (≈ line 277):
```python
async def sentinel_heartbeat_task():
    """Tell the sentinel this node is alive. Silent no-op unless FRIDAY_SENTINEL_URL is set."""
    if not settings.sentinel_url:
        return
    client = SentinelClient(settings.sentinel_url, settings.sentinel_token, settings.node_id)
    platform_summary = detect().summary
    log_info(f"Heartbeating to sentinel at {settings.sentinel_url} every {settings.heartbeat_interval_s:.0f}s")
    try:
        while not shutdown_event.is_set():
            hb = Heartbeat(status=current_system_status, version=friday.__version__,
                           platform=platform_summary,
                           meta={"remote_clients": len(remote_ws_clients)})
            await client.post(Event(type="node.heartbeat", source=settings.node_id,
                                    payload=hb.to_dict()))
            if not await sleep_unless_shutdown(settings.heartbeat_interval_s):
                break
    finally:
        await client.aclose()
```

In `run_friday`, after `telemetry_worker = asyncio.create_task(telemetry_task())` add:
```python
    heartbeat_worker = asyncio.create_task(sentinel_heartbeat_task())
```
and in the `finally:` block change both occurrences of
`(playback_task, telemetry_worker, watcher_task, agent_results_task)` to
`(playback_task, telemetry_worker, heartbeat_worker, watcher_task, agent_results_task)`.

Run: `grep -n 'heartbeat_worker' friday/desktop/hub.py`
Expected: three hits (create, cancel loop, gather).

- [ ] **Step 5: Add the client to the desktop import smoke**

In `tests/desktop/test_imports.py` append `"friday.desktop.sentinel_client"` to `MODULES`.

- [ ] **Step 6: Verify end to end**

Run: `.venv/bin/pytest -q`
Expected: all PASS, including `test_module_imports[friday.desktop.sentinel_client]`.

Manual (both processes on the Mac):
```bash
FRIDAY_SENTINEL_TOKEN=dev .venv/bin/python -m friday.sentinel &
sleep 2
FRIDAY_SENTINEL_URL=http://127.0.0.1:8770 FRIDAY_SENTINEL_TOKEN=dev FRIDAY_HEARTBEAT_INTERVAL=2 \
  timeout 12 .venv/bin/python -m friday.desktop.hub
curl -s 127.0.0.1:8770/nodes; echo
kill -TERM %1; wait
```
Expected: `/nodes` lists both the sentinel and the Mac's hostname with a status such as `Listening` or `Connecting to API`.

---

### Task 21: Cleanup, `.gitignore`, `.env.template`, README

**Files:**
- Delete: `requirements.txt`, `test_conn.py`, `test_conn_live.py`, `friday_visualization.html`, `friday_plugins/`, root `__pycache__/`
- Modify: `.gitignore`, `.env.template`, `readme.md`

- [ ] **Step 1: Delete superseded files**

```bash
git rm -q requirements.txt test_conn.py test_conn_live.py friday_visualization.html
rm -rf friday_plugins __pycache__
git status --short | head -40
```

- [ ] **Step 2: `.gitignore`**

Replace the "Local state and session handles" and "Generated 3D models" blocks with:
```gitignore
# Runtime state (FRIDAY_DATA_DIR): SQLite, memory, profiles, scenes, assets, logs
data/
```
Keep `.env`, the Python cache block, the media-capture block and `.venv/`. Add `*.egg-info/` under the Python block.

- [ ] **Step 3: `.env.template`**

Rewrite as:
```ini
# ---------------------------------------------------------------- shared
# Where every node keeps its state (SQLite, memory, profiles, generated assets).
# Default: <repo>/data
#FRIDAY_DATA_DIR=/var/lib/friday
# How this node names itself on the event bus. Default: hostname.
#FRIDAY_NODE_ID=vince-mac
FRIDAY_LOG_LEVEL=INFO

# --------------------------------------------------------------- gemini
GEMINI_API_KEY=your_gemini_api_key_here
TESTING_MODE=true

# Per-role model routing, "provider:model". Only the gemini provider ships.
# GEMINI_MODEL (legacy) still sets the Live model if FRIDAY_LLM_LIVE is unset.
GEMINI_MODEL=gemini-3.1-flash-live-preview
#FRIDAY_LLM_LIVE=gemini:gemini-3.1-flash-live-preview
#FRIDAY_LLM_AGENT_OS=gemini:gemini-3.8-flash
#FRIDAY_LLM_AGENT_SPATIAL=gemini:gemini-3.8-flash
#FRIDAY_LLM_WIDGET=gemini:gemini-3.7-flash
#FRIDAY_LLM_TRIAGE=gemini:gemini-3.7-flash

# FRIDAY's voice for the Live session: Aoede (default) or Kore.
FRIDAY_VOICE=Aoede

# Tripo3D text-to-3D (generate_spatial_3d_asset). Optional.
TRIPO_API_KEY=your_tripo_api_key_here

# -------------------------------------------------------------- sentinel
# Where the sentinel listens. Keep it on localhost; use Tailscale Serve to expose it.
FRIDAY_SENTINEL_BIND=127.0.0.1:8770
# Shared secret. If set, POST /events and /ws require "Authorization: Bearer <token>".
#FRIDAY_SENTINEL_TOKEN=change-me
# Seconds between telemetry samples / heartbeats; days of history to keep.
FRIDAY_TELEMETRY_INTERVAL=15
FRIDAY_HEARTBEAT_INTERVAL=30
FRIDAY_RETENTION_DAYS=14
# SQLite durability: FULL (default, every commit fsynced) or NORMAL (fewer fsyncs; SD cards).
FRIDAY_DB_SYNCHRONOUS=FULL
# Comma-separated dotted class paths of bridges to load. None ship yet.
#FRIDAY_BRIDGES=

# --------------------------------------------------------------- desktop
# Set on the Mac to send heartbeats to the sentinel (same token as above).
#FRIDAY_SENTINEL_URL=https://server.tailnet.ts.net/sentinel
```

- [ ] **Step 4: README**

Edit `readme.md`:

1. Under the title blockquote, replace the one-line summary with:
   > Voice-first, Gemini-only. A holographic orb you talk to on the Mac, a deck of live data widgets it composes for you, an interactive 3D spatial engine, deep native OS control — and a headless **sentinel** daemon that runs 24/7 on a home server (Fedora x86_64 today, Raspberry Pi tomorrow) as the always-on event bus the desktop and future phone/WhatsApp bridges plug into.

2. Add a new top-level section **"🛰️ Multi-node architecture"** immediately before "🏗️ System Architecture", containing: the three-package split (`friday.core` / `friday.desktop` / `friday.sentinel`) as a bullet each; the sentinel's responsibilities (durable SQLite/WAL event queue, telemetry, heartbeats, HTTP+WS API, bridges); the API table copied from `deploy/README.md`; the event envelope example; a sentence on at-least-once delivery and idempotent handlers; a pointer to `deploy/README.md` for systemd/launchd.

3. Replace the **📁 Repository Structure** tree with the new layout (from the spec §3, including `data/` and `deploy/`), and the "Generated at runtime" table's file column now lives under `data/` plus `data/sentinel.db`.

4. In **🚀 Getting Started → Installation** replace `pip install -r requirements.txt` with `pip install -e ".[desktop,dev]"` and add a "Server / Pi" variant `pip install -e ".[sentinel]"`; note Python ≥ 3.11.

5. In **Configuration** add the sentinel and routing variables (mirror `.env.template`).

6. In **Running FRIDAY** change the commands to `.venv/bin/python -m friday.desktop` (window), `.venv/bin/python -m friday.desktop.hub` (headless engine) and add `.venv/bin/python -m friday.sentinel` (sentinel) with the `curl 127.0.0.1:8770/health` check.

7. In **📱 Remote Access** change `./setup_remote.sh` to `./deploy/setup_remote.sh`.

8. In **🧩 Core Subsystems → 2. Background Agent Tiers** replace the hardcoded model column with "configured by `FRIDAY_LLM_AGENT_OS` / `FRIDAY_LLM_AGENT_SPATIAL` / `FRIDAY_LLM_WIDGET` (defaults shown)".

9. Add a **🧪 Tests** section before Tech Stack: `.venv/bin/pytest`; what the boundary test guarantees.

- [ ] **Step 5: Verify docs against source**

Run: `grep -nE 'requirements\.txt|app_desktop\.py|friday_hub\.py|python friday_hub|setup_remote\.sh' readme.md`
Expected: only the `deploy/setup_remote.sh` mention. Every `python -m` command in the README must exist: `.venv/bin/python -c "import friday.desktop.__main__, friday.sentinel.__main__"`.

---

### Task 22: Final verification

- [ ] **Step 1: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all PASS, 0 skipped on the Mac.

- [ ] **Step 2: Boundary guard**

Run: `.venv/bin/pytest tests/test_boundaries.py -v`
Expected: PASS.

- [ ] **Step 3: Sentinel foreground run + API**

```bash
FRIDAY_SENTINEL_TOKEN=dev .venv/bin/python -m friday.sentinel &
sleep 2
curl -s 127.0.0.1:8770/health | .venv/bin/python -m json.tool
curl -s -X POST 127.0.0.1:8770/events -H 'Authorization: Bearer dev' -H 'Content-Type: application/json' \
     -d '{"type":"sensor.update","source":"curl","payload":{"room":"office","temp_c":24.5}}'; echo
curl -s -X POST 127.0.0.1:8770/events -d '{"type":"a.b","source":"x"}'; echo    # 401
sleep 1; curl -s 127.0.0.1:8770/telemetry | head -c 300; echo
kill -TERM %1; wait; echo "sentinel exit=$?"
```
Expected: health JSON; `{"ids":[...]}` with 202; `{"error":"missing or invalid bearer token"}`; a telemetry snapshot with `cpu_percent`, `thermal` (empty on macOS) and `platform`; exit 0 after `stopped`.

- [ ] **Step 4: Desktop boot**

Run: `.venv/bin/python -m friday.desktop` and confirm the window opens, the orb reaches Idle/Listening, and closing the window exits cleanly. (Skip the voice round-trip; import + boot is the scope.)

- [ ] **Step 5: Working tree summary**

Run: `git status --short | wc -l && git diff --stat | tail -1`
Report the counts and leave everything uncommitted for the user.
