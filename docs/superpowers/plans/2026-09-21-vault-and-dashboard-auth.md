# Vault, Settings API and Dashboard Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sentinel the configuration authority: secrets AES-GCM-encrypted in SQLite under `FRIDAY_MASTER_KEY`, a single-user login with hashed sessions, vault-managed node tokens, a schema-driven settings API with audit, the first three dashboard pages, and a desktop that pulls its LLM config from the sentinel with `.env` fallback.

**Architecture:** `friday.core.vault` (crypto) and storage schema v2 sit in core. The sentinel gains a code-declared settings registry, a `RuntimeConfig` that fronts the vault with `vault → env → default` precedence, an `auth` module (scrypt, sessions, node tokens, lockout), a `principals` resolver used by both the node API and the new `web.py` dashboard routes, a CLI for bootstrap, and static dashboard files. The desktop pulls `GET /config?scope=desktop` at boot and overlays it onto `Settings`.

**Tech Stack:** Python ≥ 3.11, stdlib `sqlite3`/`hashlib.scrypt`/`hmac`, `cryptography` (AES-GCM + HKDF), `aiohttp`, vanilla HTML/JS/CSS; `pytest` + `pytest-asyncio` + `pytest-aiohttp`.

**Spec:** `docs/superpowers/specs/2026-09-21-vault-and-dashboard-auth-design.md` (roadmap: `docs/superpowers/specs/2026-09-21-sentinel-evolution-roadmap.md`)

## Global Constraints

- **No commits.** The user's global rule. Leave all work in the working tree on `feat/multi-node-scaffold`.
- Python `>=3.11`; no 3.12+ syntax.
- `friday.core` and `friday.sentinel` never import `Quartz`, `pyaudio`, `cv2`, `webview`, `EventKit`, `Foundation`, `AppKit`, `objc`, `pyautogui`, `termios`, `tty`, `PIL`, `numpy`. `tests/test_boundaries.py` must stay green after every task. `cryptography` is allowed (base dependency).
- Secrets never appear in logs, events, audit detail or API responses; masked as `{"set": true, "hint": "…"}`.
- SQLite stays WAL; migrations forward-only; multi-statement changes use explicit `BEGIN`/`COMMIT`.
- Use `.venv/bin/python` / `.venv/bin/pytest` for everything.
- Names in a task's **Interfaces → Produces** block are contracts for later tasks; keep them exact.
- Two small deviations from the spec's module listing, both to avoid an import cycle: `resolve_principal` lives in `friday/sentinel/principals.py` (not `auth.py`), and `HealthState` moves to `friday/sentinel/services.py` (re-exported from `api.py` so existing imports keep working). `SettingSpec` gains a `validator` field the spec text implies ("route values are validated with `parse_route`").

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `friday/core/vault.py` | `Vault`, `MasterKeyError`, `VaultError` |
| `friday/sentinel/settings_registry.py` | `SettingSpec`, `REGISTRY`, `spec_for`, `validate`, `schema`, `SettingValidationError` |
| `friday/sentinel/runtime_config.py` | `RuntimeConfig`, `legacy_value` |
| `friday/sentinel/auth.py` | password hashing, tokens, `SessionManager`, `NodeTokens`, `LoginLimiter`, `client_ip`, `is_https` |
| `friday/sentinel/services.py` | `HealthState`, `Services`, `SERVICES` app key |
| `friday/sentinel/principals.py` | `resolve_principal`, `require_principal`, `require_user`, `unauthorized()` |
| `friday/sentinel/web.py` | `/auth/*`, `/api/*`, `/config`, static + shell routes |
| `friday/sentinel/cli.py` | `keygen`, `user set-password`, `token create/list/revoke`, `run` |
| `friday/sentinel/dashboard/{index.html,app.js,style.css}` | login / settings / tokens views |
| `friday/desktop/config_pull.py` | `pull_or_cached` |
| `tests/sentinel/conftest.py` | `build_services`, `services`, `login`, `node_headers` fixtures |
| `tests/core/test_vault.py`, `tests/core/test_storage_v2.py`, `tests/sentinel/test_settings_registry.py`, `tests/sentinel/test_runtime_config.py`, `tests/sentinel/test_auth.py`, `tests/sentinel/test_web.py`, `tests/sentinel/test_cli.py`, `tests/desktop/test_config_pull.py` | per-task tests |

**Modified**

`pyproject.toml`, `friday/core/config.py`, `friday/core/storage.py`, `friday/sentinel/api.py`, `friday/sentinel/daemon.py`, `friday/sentinel/monitors.py`, `friday/sentinel/__main__.py`, `friday/desktop/sentinel_client.py`, `friday/desktop/hub.py`, `friday/desktop/agents.py`, `friday/desktop/widget_generator.py`, `tests/conftest.py`, `tests/core/test_config.py`, `tests/sentinel/test_api.py`, `tests/sentinel/test_daemon.py`, `tests/desktop/test_sentinel_client.py`, `tests/desktop/test_imports.py`, `.env.template`, `deploy/README.md`, `readme.md`.

---

### Task 1: Bootstrap config fields, `override_settings`, `apply_overrides`

**Files:**
- Modify: `friday/core/config.py`, `pyproject.toml`, `tests/conftest.py`
- Test: `tests/core/test_config.py` (append)

**Interfaces:**
- Produces on `Settings`: `master_key: str | None`, `trusted_proxy: bool`, `env: Mapping[str, str]` (only keys starting `FRIDAY_`, `GEMINI_`, `TRIPO_`).
- Produces: `override_settings(settings: Settings) -> None`; `get_settings()` returns the override once set; `get_settings.cache_clear()` also clears the override.
- Produces: `CONFIG_FIELD_MAP: Mapping[str, str]`, `ROUTE_KEY_PREFIX = "llm.routes."`, `apply_overrides(settings: Settings, values: Mapping[str, Any]) -> Settings`.
- Produces in `tests/conftest.py`: `TEST_MASTER_KEY: str` and `make_settings` now defaults `FRIDAY_MASTER_KEY` to it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config.py`:
```python
from friday.core import config as config_module
from friday.core.config import apply_overrides, override_settings


def test_master_key_and_trusted_proxy(make_settings):
    s = make_settings(FRIDAY_MASTER_KEY="abc", FRIDAY_TRUSTED_PROXY="true")
    assert s.master_key == "abc"
    assert s.trusted_proxy is True
    assert make_settings(FRIDAY_MASTER_KEY="").master_key is None
    assert make_settings().trusted_proxy is False


def test_trusted_proxy_rejects_garbage(make_settings):
    with pytest.raises(ConfigError) as excinfo:
        make_settings(FRIDAY_TRUSTED_PROXY="maybe")
    assert "FRIDAY_TRUSTED_PROXY" in str(excinfo.value)


def test_env_is_filtered_to_friday_prefixes(tmp_path):
    s = load_settings(env={"FRIDAY_DATA_DIR": str(tmp_path / "d"), "GEMINI_API_KEY": "k",
                           "TRIPO_API_KEY": "t", "PATH": "/usr/bin", "HOME": "/x"},
                      env_file=tmp_path / "absent.env")
    assert dict(s.env) == {"FRIDAY_DATA_DIR": str(tmp_path / "d"), "GEMINI_API_KEY": "k", "TRIPO_API_KEY": "t"}


def test_override_settings_wins_until_cleared(monkeypatch, tmp_path, make_settings):
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "d"))
    config_module.get_settings.cache_clear()
    try:
        custom = make_settings(FRIDAY_NODE_ID="overridden")
        override_settings(custom)
        assert config_module.get_settings() is custom
        config_module.get_settings.cache_clear()
        assert config_module.get_settings().node_id != "overridden"
    finally:
        config_module.get_settings.cache_clear()


def test_apply_overrides_maps_registry_keys(make_settings):
    s = make_settings(GEMINI_API_KEY="old", FRIDAY_VOICE="Aoede")
    out = apply_overrides(s, {
        "llm.gemini_api_key": "new-key",
        "llm.tripo_api_key": "tripo",
        "desktop.voice": "Kore",
        "llm.routes.live": "gemini:live-x",
        "llm.routes.widget": "gemini:widget-x",
        "llm.routes.nonsense": "ignored",
        "unknown.key": "ignored",
        "llm.routes.triage": None,
    })
    assert out.gemini_api_key == "new-key"
    assert out.tripo_api_key == "tripo"
    assert out.friday_voice == "Kore"
    assert out.llm_routes["live"] == "gemini:live-x"
    assert out.llm_routes["widget"] == "gemini:widget-x"
    assert out.llm_routes["triage"] == s.llm_routes["triage"]
    assert "nonsense" not in out.llm_routes
    assert out.node_id == s.node_id                     # untouched fields survive
    assert s.gemini_api_key == "old"                    # input is not mutated
```

Edit `tests/conftest.py` so `make_settings` seeds a master key:
```python
"""Shared fixtures. Everything runs against tmp_path; nothing touches <repo>/data."""
from pathlib import Path

import pytest

from friday.core.config import Settings, load_settings

# One master key for the whole test session; every sentinel fixture inherits it.
TEST_MASTER_KEY = "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmctISEhIQ=="   # base64 of 32 bytes


@pytest.fixture
def make_settings(tmp_path: Path):
    """Build Settings from an explicit env dict, isolated from the real .env."""

    def _make(**env: str) -> Settings:
        env.setdefault("FRIDAY_DATA_DIR", str(tmp_path / "data"))
        env.setdefault("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
        return load_settings(env=env, env_file=tmp_path / "absent.env")

    return _make
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_config.py -q`
Expected: failures on `master_key`, `trusted_proxy`, `env`, `override_settings`, `apply_overrides`.

- [ ] **Step 3: Implement**

In `friday/core/config.py`:

Add to the imports: `import dataclasses` and `from typing import Any, Mapping`.

Add after `SYNC_MODES`:
```python
ENV_PREFIXES = ("FRIDAY_", "GEMINI_", "TRIPO_")

# Registry keys the desktop may receive from the sentinel, mapped onto Settings.
CONFIG_FIELD_MAP: Mapping[str, str] = {
    "llm.gemini_api_key": "gemini_api_key",
    "llm.tripo_api_key": "tripo_api_key",
    "desktop.voice": "friday_voice",
}
ROUTE_KEY_PREFIX = "llm.routes."
```

Add three fields at the end of `Settings`:
```python
    master_key: str | None
    trusted_proxy: bool
    env: Mapping[str, str]
```

In `load_settings`, add to the `Settings(...)` constructor call:
```python
        master_key=_optional(merged, "FRIDAY_MASTER_KEY"),
        trusted_proxy=_bool(merged, "FRIDAY_TRUSTED_PROXY", False),
        env={k: v for k, v in merged.items() if k.startswith(ENV_PREFIXES)},
```

Replace the `get_settings` definition with:
```python
_override: Settings | None = None


@lru_cache(maxsize=1)
def _load_cached() -> Settings:
    return load_settings()


def get_settings() -> Settings:
    """Process-wide Settings for modules that cannot be handed one explicitly."""
    return _override if _override is not None else _load_cached()


def override_settings(settings: Settings) -> None:
    """Make ``get_settings()`` return ``settings`` for the rest of the process
    (the desktop calls this after pulling configuration from the sentinel)."""
    global _override
    _override = settings


def _clear_settings_cache() -> None:
    global _override
    _override = None
    _load_cached.cache_clear()


get_settings.cache_clear = _clear_settings_cache      # type: ignore[attr-defined]


def apply_overrides(settings: Settings, values: Mapping[str, Any]) -> Settings:
    """Overlay registry-keyed values (from GET /config) onto Settings. Unknown
    keys and None values are ignored; the input is not mutated."""
    routes = dict(settings.llm_routes)
    changes: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key.startswith(ROUTE_KEY_PREFIX):
            role = key[len(ROUTE_KEY_PREFIX):]
            if role in LLM_ROLES:
                routes[role] = str(value)
        elif key in CONFIG_FIELD_MAP:
            changes[CONFIG_FIELD_MAP[key]] = value
    return dataclasses.replace(settings, llm_routes=routes, **changes)
```

Add the helper next to `_int`:
```python
def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")
```

In `pyproject.toml` add `"cryptography",` to `[project] dependencies` (after `"google-genai",`).

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pip install -e ".[desktop,dev]" -q && .venv/bin/pytest tests/core -q`
Expected: all PASS (existing config tests included).

---

### Task 2: `friday.core.vault`

**Files:**
- Create: `friday/core/vault.py`
- Test: `tests/core/test_vault.py`

**Interfaces:**
- Produces: `class MasterKeyError(ConfigError)`, `class VaultError(ValueError)`, `class Vault` with `Vault.from_master_key(encoded: str) -> Vault`, `Vault.generate_master_key() -> str`, `encrypt(key: str, plaintext: str) -> str`, `decrypt(key: str, token: str) -> str`, `fingerprint() -> str`, `VERSION = "v1"`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_vault.py`:
```python
import base64

import pytest

from friday.core.vault import MasterKeyError, Vault, VaultError


def test_generate_master_key_is_32_bytes_urlsafe():
    key = Vault.generate_master_key()
    assert len(base64.urlsafe_b64decode(key)) == 32
    assert Vault.generate_master_key() != key


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.b64encode(b"short").decode()])
def test_short_or_malformed_master_key_rejected(bad):
    with pytest.raises(MasterKeyError) as excinfo:
        Vault.from_master_key(bad)
    assert "FRIDAY_MASTER_KEY" in str(excinfo.value)


def test_standard_and_urlsafe_base64_both_accepted():
    raw = bytes(range(32))
    a = Vault.from_master_key(base64.b64encode(raw).decode())
    b = Vault.from_master_key(base64.urlsafe_b64encode(raw).decode())
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 8


def test_roundtrip_and_token_format():
    v = Vault.from_master_key(Vault.generate_master_key())
    token = v.encrypt("llm.gemini_api_key", "s3cret")
    assert token.startswith("v1:") and token.count(":") == 2
    assert v.decrypt("llm.gemini_api_key", token) == "s3cret"
    assert v.encrypt("llm.gemini_api_key", "s3cret") != token      # fresh nonce every time


def test_wrong_master_key_fails():
    a = Vault.from_master_key(Vault.generate_master_key())
    b = Vault.from_master_key(Vault.generate_master_key())
    with pytest.raises(VaultError):
        b.decrypt("k", a.encrypt("k", "x"))


def test_setting_key_is_bound_as_aad():
    v = Vault.from_master_key(Vault.generate_master_key())
    token = v.encrypt("llm.tripo_api_key", "x")
    with pytest.raises(VaultError):
        v.decrypt("llm.gemini_api_key", token)


def test_tampered_ciphertext_fails():
    v = Vault.from_master_key(Vault.generate_master_key())
    version, nonce, ct = v.encrypt("k", "hello").split(":")
    raw = bytearray(base64.b64decode(ct))
    raw[0] ^= 0x01
    with pytest.raises(VaultError):
        v.decrypt("k", f"{version}:{nonce}:{base64.b64encode(bytes(raw)).decode()}")


@pytest.mark.parametrize("bad", ["", "v1:", "v1:abc", "v2:AAAA:BBBB", "plain text", "v1:!!:!!"])
def test_malformed_tokens_fail_cleanly(bad):
    v = Vault.from_master_key(Vault.generate_master_key())
    with pytest.raises(VaultError):
        v.decrypt("k", bad)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_vault.py -q`
Expected: `ModuleNotFoundError: friday.core.vault`.

- [ ] **Step 3: Implement**

`friday/core/vault.py`:
```python
"""Secrets at rest: AES-GCM under a key derived from FRIDAY_MASTER_KEY.

The setting key is bound in as associated data, so a ciphertext copied into
another setting's row cannot be decrypted there. Tokens are versioned
(``v1:``) so a future key rotation or algorithm change can coexist with old
rows during a migration.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from friday.core.config import ConfigError

MIN_MASTER_KEY_BYTES = 32
_HKDF_INFO = b"friday-vault-v1"


class MasterKeyError(ConfigError):
    """FRIDAY_MASTER_KEY is missing, malformed, or too short."""


class VaultError(ValueError):
    """A token could not be decrypted: wrong key, tampered, or malformed."""


def _b64decode_any(text: str) -> bytes:
    text = text.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as e:
        raise ValueError(str(e)) from e


class Vault:
    VERSION = "v1"

    def __init__(self, derived_key: bytes):
        self._aead = AESGCM(derived_key)
        self._fingerprint = hashlib.sha256(derived_key).hexdigest()[:8]

    @classmethod
    def from_master_key(cls, encoded: str) -> "Vault":
        if not encoded or not encoded.strip():
            raise MasterKeyError(
                "FRIDAY_MASTER_KEY is not set — run 'friday-sentinel keygen' and add the printed line to .env")
        try:
            raw = _b64decode_any(encoded)
        except ValueError:
            raise MasterKeyError("FRIDAY_MASTER_KEY is not valid base64") from None
        if len(raw) < MIN_MASTER_KEY_BYTES:
            raise MasterKeyError(
                f"FRIDAY_MASTER_KEY must decode to at least {MIN_MASTER_KEY_BYTES} bytes, got {len(raw)}")
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(raw)
        return cls(derived)

    @staticmethod
    def generate_master_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(MIN_MASTER_KEY_BYTES)).decode()

    def fingerprint(self) -> str:
        return self._fingerprint

    def encrypt(self, key: str, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), key.encode("utf-8"))
        return f"{self.VERSION}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"

    def decrypt(self, key: str, token: str) -> str:
        parts = token.split(":")
        if len(parts) != 3 or parts[0] != self.VERSION:
            raise VaultError("unrecognised vault token format")
        try:
            nonce = base64.b64decode(parts[1], validate=True)
            ct = base64.b64decode(parts[2], validate=True)
        except (binascii.Error, ValueError) as e:
            raise VaultError(f"malformed vault token: {e}") from e
        if len(nonce) != 12:
            raise VaultError("malformed vault token: bad nonce length")
        try:
            return self._aead.decrypt(nonce, ct, key.encode("utf-8")).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as e:
            raise VaultError("cannot decrypt: wrong master key, wrong setting key, or tampered data") from e
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/test_vault.py tests/test_boundaries.py -q`
Expected: all PASS (the boundary test proves `cryptography` imports cleanly under the poison list).

---

### Task 3: Storage schema v2

**Files:**
- Modify: `friday/core/storage.py`
- Test: `tests/core/test_storage_v2.py`

**Interfaces:**
- Produces dataclasses: `SettingRow(key, value, secret: bool, updated_at, updated_by)`, `UserRow(username, password_hash, created_at, password_changed_at)`, `SessionRow(id, username, created_at, expires_at, last_seen, user_agent, ip)`, `NodeTokenRow(id, name, created_at, last_used, revoked_at)`, `AuditRow(id, ts, actor, action, target, detail: dict)`.
- Produces on `Store` and (async twins) `AsyncStore`: `setting_get(key)`, `setting_set(key, value, *, secret, updated_by, ts)`, `setting_delete(key) -> bool`, `settings_all()`, `user_get(username)`, `user_upsert(username, password_hash, ts)`, `users_count() -> int`, `session_create(id, username, ts, expires_at, user_agent, ip)`, `session_get(id)`, `session_touch(id, ts)`, `session_delete(id) -> bool`, `sessions_prune(now) -> int`, `node_token_create(id, name, token_hash, ts)`, `node_token_by_hash(token_hash)`, `node_token_touch(id, ts)`, `node_tokens_list()`, `node_token_revoke(id, ts) -> bool`, `audit_append(ts, actor, action, target, detail) -> int`, `audit_list(limit=100, before_id=None)`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_storage_v2.py`:
```python
import pytest

from friday.core import storage
from friday.core.events import Event
from friday.core.storage import AsyncStore, Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v1_database_upgrades_to_v2_with_rows_intact(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {1: storage.MIGRATIONS[1]})
    old = Store.open(path)
    old.kv_set("k", 1)
    old.enqueue(Event(type="a.b", source="s", id="keep"))
    assert old.schema_version() == 1
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 2
        assert s.kv_get("k") == 1
        assert s.list_events()[0].event.id == "keep"
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"settings", "users", "sessions", "node_tokens", "audit"} <= names
        assert s.users_count() == 0
    finally:
        s.close()


def test_settings_crud(store):
    assert store.setting_get("x") is None
    store.setting_set("llm.k", "v1:abc:def", secret=True, updated_by="cli", ts=1.0)
    store.setting_set("controls.dnd", "true", secret=False, updated_by="dashboard:vince", ts=2.0)
    row = store.setting_get("llm.k")
    assert row.value == "v1:abc:def" and row.secret is True and row.updated_by == "cli" and row.updated_at == 1.0
    store.setting_set("llm.k", "v1:new", secret=True, updated_by="cli", ts=3.0)
    assert store.setting_get("llm.k").value == "v1:new"
    assert [r.key for r in store.settings_all()] == ["controls.dnd", "llm.k"]
    assert store.setting_delete("llm.k") is True
    assert store.setting_delete("llm.k") is False
    assert store.setting_get("llm.k") is None


def test_users(store):
    assert store.user_get("vince") is None
    store.user_upsert("vince", "scrypt$1", ts=10.0)
    store.user_upsert("vince", "scrypt$2", ts=20.0)
    u = store.user_get("vince")
    assert u.password_hash == "scrypt$2" and u.created_at == 10.0 and u.password_changed_at == 20.0
    assert store.users_count() == 1


def test_sessions(store):
    store.session_create("h1", "vince", ts=1.0, expires_at=100.0, user_agent="ua", ip="1.2.3.4")
    store.session_create("h2", "vince", ts=1.0, expires_at=5.0, user_agent=None, ip=None)
    s = store.session_get("h1")
    assert s.username == "vince" and s.last_seen == 1.0 and s.user_agent == "ua" and s.ip == "1.2.3.4"
    store.session_touch("h1", ts=50.0)
    assert store.session_get("h1").last_seen == 50.0
    assert store.sessions_prune(now=10.0) == 1            # h2 expired, h1 kept
    assert store.session_get("h2") is None
    assert store.session_delete("h1") is True
    assert store.session_delete("h1") is False


def test_node_tokens(store):
    store.node_token_create("id1", "desktop", "hash1", ts=1.0)
    store.node_token_create("id2", "phone", "hash2", ts=2.0)
    row = store.node_token_by_hash("hash1")
    assert row.id == "id1" and row.name == "desktop" and row.last_used is None and row.revoked_at is None
    assert not hasattr(row, "token_hash")
    store.node_token_touch("id1", ts=5.0)
    assert store.node_token_by_hash("hash1").last_used == 5.0
    assert store.node_token_revoke("id1", ts=6.0) is True
    assert store.node_token_revoke("id1", ts=6.0) is False
    assert store.node_token_by_hash("hash1") is None
    listed = {r.id: r for r in store.node_tokens_list()}
    assert listed["id1"].revoked_at == 6.0 and listed["id2"].revoked_at is None
    assert store.node_token_by_hash("nope") is None


def test_audit(store):
    ids = [store.audit_append(ts=float(i), actor="cli", action="settings.update", target=f"k{i}", detail={"i": i})
           for i in range(5)]
    assert ids == sorted(ids)
    rows = store.audit_list(limit=3)
    assert [r.target for r in rows] == ["k4", "k3", "k2"]
    assert rows[0].detail == {"i": 4}
    older = store.audit_list(limit=10, before_id=rows[-1].id)
    assert [r.target for r in older] == ["k1", "k0"]


async def test_async_twins(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        await s.setting_set("k", "v", secret=False, updated_by="t", ts=1.0)
        assert (await s.setting_get("k")).value == "v"
        assert [r.key for r in await s.settings_all()] == ["k"]
        await s.user_upsert("u", "h", ts=1.0)
        assert (await s.user_get("u")).password_hash == "h"
        assert await s.users_count() == 1
        await s.session_create("sid", "u", ts=1.0, expires_at=2.0, user_agent=None, ip=None)
        assert (await s.session_get("sid")).username == "u"
        await s.session_touch("sid", ts=1.5)
        assert await s.sessions_prune(now=3.0) == 1
        assert await s.session_delete("sid") is False
        await s.node_token_create("n", "name", "hash", ts=1.0)
        assert (await s.node_token_by_hash("hash")).name == "name"
        await s.node_token_touch("n", ts=2.0)
        assert len(await s.node_tokens_list()) == 1
        assert await s.node_token_revoke("n", ts=3.0) is True
        assert await s.audit_append(ts=1.0, actor="a", action="b", target=None, detail={}) == 1
        assert len(await s.audit_list()) == 1
        assert await s.setting_delete("k") is True
    finally:
        await s.aclose()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_storage_v2.py -q`
Expected: failures — `schema_version() == 1`, missing methods.

- [ ] **Step 3: Implement**

In `friday/core/storage.py`:

Add migration 2 to `MIGRATIONS` (after the `1:` entry):
```python
    2: (
        "CREATE TABLE settings ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL DEFAULT 0,"
        "  updated_at REAL NOT NULL, updated_by TEXT NOT NULL)",
        "CREATE TABLE users ("
        "  username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,"
        "  created_at REAL NOT NULL, password_changed_at REAL NOT NULL)",
        "CREATE TABLE sessions ("
        "  id TEXT PRIMARY KEY, username TEXT NOT NULL, created_at REAL NOT NULL,"
        "  expires_at REAL NOT NULL, last_seen REAL NOT NULL, user_agent TEXT, ip TEXT)",
        "CREATE INDEX sessions_expires ON sessions(expires_at)",
        "CREATE TABLE node_tokens ("
        "  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,"
        "  created_at REAL NOT NULL, last_used REAL, revoked_at REAL)",
        "CREATE TABLE audit ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,"
        "  action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL)",
        "CREATE INDEX audit_ts ON audit(ts DESC)",
    ),
```

Add the row dataclasses after `StoredEvent`:
```python
@dataclass(frozen=True)
class SettingRow:
    key: str
    value: str
    secret: bool
    updated_at: float
    updated_by: str


@dataclass(frozen=True)
class UserRow:
    username: str
    password_hash: str
    created_at: float
    password_changed_at: float


@dataclass(frozen=True)
class SessionRow:
    id: str
    username: str
    created_at: float
    expires_at: float
    last_seen: float
    user_agent: str | None
    ip: str | None


@dataclass(frozen=True)
class NodeTokenRow:
    id: str
    name: str
    created_at: float
    last_used: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: float
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]
```

Add these methods to `Store` (after the telemetry section):
```python
    # -------------------------------------------------------------- settings

    def setting_get(self, key: str) -> SettingRow | None:
        row = self._conn.execute(
            "SELECT key, value, secret, updated_at, updated_by FROM settings WHERE key = ?",
            (key,)).fetchone()
        return None if row is None else SettingRow(
            row["key"], row["value"], bool(row["secret"]), row["updated_at"], row["updated_by"])

    def setting_set(self, key: str, value: str, *, secret: bool, updated_by: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value, secret, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, secret = excluded.secret, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, int(secret), ts, updated_by))

    def setting_delete(self, key: str) -> bool:
        return self._conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0

    def settings_all(self) -> list[SettingRow]:
        rows = self._conn.execute(
            "SELECT key, value, secret, updated_at, updated_by FROM settings ORDER BY key").fetchall()
        return [SettingRow(r["key"], r["value"], bool(r["secret"]), r["updated_at"], r["updated_by"])
                for r in rows]

    # ----------------------------------------------------------------- users

    def user_get(self, username: str) -> UserRow | None:
        row = self._conn.execute(
            "SELECT username, password_hash, created_at, password_changed_at FROM users WHERE username = ?",
            (username,)).fetchone()
        return None if row is None else UserRow(
            row["username"], row["password_hash"], row["created_at"], row["password_changed_at"])

    def user_upsert(self, username: str, password_hash: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO users (username, password_hash, created_at, password_changed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
            "password_hash = excluded.password_hash, password_changed_at = excluded.password_changed_at",
            (username, password_hash, ts, ts))

    def users_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # -------------------------------------------------------------- sessions

    def session_create(self, id: str, username: str, ts: float, expires_at: float,
                       user_agent: str | None, ip: str | None) -> None:
        self._conn.execute(
            "INSERT INTO sessions (id, username, created_at, expires_at, last_seen, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, username, ts, expires_at, ts, user_agent, ip))

    def session_get(self, id: str) -> SessionRow | None:
        row = self._conn.execute(
            "SELECT id, username, created_at, expires_at, last_seen, user_agent, ip "
            "FROM sessions WHERE id = ?", (id,)).fetchone()
        return None if row is None else SessionRow(
            row["id"], row["username"], row["created_at"], row["expires_at"],
            row["last_seen"], row["user_agent"], row["ip"])

    def session_touch(self, id: str, ts: float) -> None:
        self._conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (ts, id))

    def session_delete(self, id: str) -> bool:
        return self._conn.execute("DELETE FROM sessions WHERE id = ?", (id,)).rowcount > 0

    def sessions_prune(self, now: float) -> int:
        return self._conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount

    # ----------------------------------------------------------- node tokens

    def node_token_create(self, id: str, name: str, token_hash: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO node_tokens (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (id, name, token_hash, ts))

    def node_token_by_hash(self, token_hash: str) -> NodeTokenRow | None:
        row = self._conn.execute(
            "SELECT id, name, created_at, last_used, revoked_at FROM node_tokens "
            "WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)).fetchone()
        return None if row is None else NodeTokenRow(
            row["id"], row["name"], row["created_at"], row["last_used"], row["revoked_at"])

    def node_token_touch(self, id: str, ts: float) -> None:
        self._conn.execute("UPDATE node_tokens SET last_used = ? WHERE id = ?", (ts, id))

    def node_tokens_list(self) -> list[NodeTokenRow]:
        rows = self._conn.execute(
            "SELECT id, name, created_at, last_used, revoked_at FROM node_tokens ORDER BY created_at").fetchall()
        return [NodeTokenRow(r["id"], r["name"], r["created_at"], r["last_used"], r["revoked_at"])
                for r in rows]

    def node_token_revoke(self, id: str, ts: float) -> bool:
        return self._conn.execute(
            "UPDATE node_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (ts, id)).rowcount > 0

    # ----------------------------------------------------------------- audit

    def audit_append(self, ts: float, actor: str, action: str, target: str | None,
                     detail: dict[str, Any]) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (ts, actor, action, target, json.dumps(detail)))
        return int(cur.lastrowid)

    def audit_list(self, limit: int = 100, before_id: int | None = None) -> list[AuditRow]:
        if before_id is None:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, target, detail FROM audit ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, target, detail FROM audit WHERE id < ? "
                "ORDER BY id DESC LIMIT ?", (before_id, limit)).fetchall()
        return [AuditRow(r["id"], r["ts"], r["actor"], r["action"], r["target"], json.loads(r["detail"]))
                for r in rows]
```

Add the async twins to `AsyncStore` (after `prune`):
```python
    async def setting_get(self, key: str) -> SettingRow | None:
        return await self.run(self._store.setting_get, key)

    async def setting_set(self, key: str, value: str, *, secret: bool, updated_by: str, ts: float) -> None:
        await self.run(self._store.setting_set, key, value, secret=secret, updated_by=updated_by, ts=ts)

    async def setting_delete(self, key: str) -> bool:
        return await self.run(self._store.setting_delete, key)

    async def settings_all(self) -> list[SettingRow]:
        return await self.run(self._store.settings_all)

    async def user_get(self, username: str) -> UserRow | None:
        return await self.run(self._store.user_get, username)

    async def user_upsert(self, username: str, password_hash: str, ts: float) -> None:
        await self.run(self._store.user_upsert, username, password_hash, ts)

    async def users_count(self) -> int:
        return await self.run(self._store.users_count)

    async def session_create(self, id: str, username: str, ts: float, expires_at: float,
                             user_agent: str | None, ip: str | None) -> None:
        await self.run(self._store.session_create, id, username, ts, expires_at, user_agent, ip)

    async def session_get(self, id: str) -> SessionRow | None:
        return await self.run(self._store.session_get, id)

    async def session_touch(self, id: str, ts: float) -> None:
        await self.run(self._store.session_touch, id, ts)

    async def session_delete(self, id: str) -> bool:
        return await self.run(self._store.session_delete, id)

    async def sessions_prune(self, now: float) -> int:
        return await self.run(self._store.sessions_prune, now)

    async def node_token_create(self, id: str, name: str, token_hash: str, ts: float) -> None:
        await self.run(self._store.node_token_create, id, name, token_hash, ts)

    async def node_token_by_hash(self, token_hash: str) -> NodeTokenRow | None:
        return await self.run(self._store.node_token_by_hash, token_hash)

    async def node_token_touch(self, id: str, ts: float) -> None:
        await self.run(self._store.node_token_touch, id, ts)

    async def node_tokens_list(self) -> list[NodeTokenRow]:
        return await self.run(self._store.node_tokens_list)

    async def node_token_revoke(self, id: str, ts: float) -> bool:
        return await self.run(self._store.node_token_revoke, id, ts)

    async def audit_append(self, ts: float, actor: str, action: str, target: str | None,
                           detail: dict[str, Any]) -> int:
        return await self.run(self._store.audit_append, ts, actor, action, target, detail)

    async def audit_list(self, limit: int = 100, before_id: int | None = None) -> list[AuditRow]:
        return await self.run(self._store.audit_list, limit, before_id)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core -q`
Expected: all PASS, including the existing v1 storage tests (schema version assertions there check `== 1` — update `test_open_creates_parent_and_schema` and `test_reopen_is_idempotent` in `tests/core/test_storage.py` to `== 2`, and `test_corrupt_file_is_quarantined` likewise).

---

### Task 4: Settings registry

**Files:**
- Create: `friday/sentinel/settings_registry.py`
- Test: `tests/sentinel/test_settings_registry.py`

**Interfaces:**
- Produces: `class SettingValidationError(ValueError)`, `@dataclass(frozen=True) class SettingSpec(key, type, group, description, default=None, secret=False, choices=(), scopes=(), env=None, restart_required=False, validator=None)`, `REGISTRY: tuple[SettingSpec, ...]`, `spec_for(key) -> SettingSpec` (raises `KeyError`), `validate(spec, value) -> Any`, `schema() -> list[dict]` (groups with keys; no values), `GROUP_ORDER`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_settings_registry.py`:
```python
import json

import pytest

from friday.sentinel.settings_registry import (REGISTRY, SettingSpec, SettingValidationError,
                                               schema, spec_for, validate)


def test_registry_keys_are_unique_and_dotted():
    keys = [s.key for s in REGISTRY]
    assert len(keys) == len(set(keys))
    assert all("." in k and k == k.lower() for k in keys)


def test_initial_registry_contents():
    assert spec_for("llm.gemini_api_key").secret is True
    assert spec_for("llm.gemini_api_key").scopes == ("desktop",)
    assert spec_for("llm.gemini_api_key").env == "GEMINI_API_KEY"
    assert spec_for("llm.routes.triage").scopes == ()
    assert spec_for("llm.routes.widget").default == "gemini:gemini-3.7-flash"
    assert spec_for("controls.call_mode").choices == ("always", "urgent_only", "mute")
    assert spec_for("controls.call_mode").default == "urgent_only"
    assert spec_for("controls.dnd").default is False
    assert spec_for("desktop.voice").env == "FRIDAY_VOICE"
    with pytest.raises(KeyError):
        spec_for("no.such")


@pytest.mark.parametrize("key,raw,expected", [
    ("llm.gemini_api_key", "  abc  ", "abc"),
    ("llm.routes.live", " Gemini : m ", "gemini:m"),
    ("controls.call_mode", "mute", "mute"),
    ("controls.dnd", "true", True),
    ("controls.dnd", False, False),
    ("controls.dnd", "0", False),
])
def test_validate_coerces(key, raw, expected):
    assert validate(spec_for(key), raw) == expected


@pytest.mark.parametrize("key,raw", [
    ("llm.gemini_api_key", 123),
    ("llm.gemini_api_key", ""),
    ("llm.routes.live", "nocolon"),
    ("controls.call_mode", "sometimes"),
    ("controls.dnd", "maybe"),
    ("controls.dnd", 2),
])
def test_validate_rejects(key, raw):
    with pytest.raises(SettingValidationError) as excinfo:
        validate(spec_for(key), raw)
    assert key in str(excinfo.value)


def test_generic_types():
    assert validate(SettingSpec("t.i", "int", "t", ""), "42") == 42
    assert validate(SettingSpec("t.f", "float", "t", ""), "1.5") == 1.5
    assert validate(SettingSpec("t.u", "url", "t", ""), "https://x.example/api") == "https://x.example/api"
    assert validate(SettingSpec("t.l", "list", "t", ""), "a, b,,c") == ["a", "b", "c"]
    assert validate(SettingSpec("t.l", "list", "t", ""), ["x", "y"]) == ["x", "y"]
    for spec, bad in [(SettingSpec("t.i", "int", "t", ""), "x"), (SettingSpec("t.i", "int", "t", ""), True),
                      (SettingSpec("t.u", "url", "t", ""), "ftp://x"), (SettingSpec("t.l", "list", "t", ""), [1])]:
        with pytest.raises(SettingValidationError):
            validate(spec, bad)


def test_schema_has_groups_and_no_values():
    groups = schema()
    assert [g["name"] for g in groups] == ["llm", "desktop", "controls"]
    flat = json.dumps(groups)
    assert "value" not in flat
    key = next(k for g in groups for k in g["keys"] if k["key"] == "controls.call_mode")
    assert key["type"] == "enum" and key["choices"] == ["always", "urgent_only", "mute"]
    assert key["secret"] is False and "description" in key
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_settings_registry.py -q`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`friday/sentinel/settings_registry.py`:
```python
"""The declared shape of every dynamic setting.

The dashboard renders forms from ``schema()``; the server validates writes
with ``validate()``; ``RuntimeConfig`` uses ``scopes`` to decide what a node
may pull. Later sub-projects append groups (jira, google, whatsapp,
telephony, controls.monitors); nothing else in the codebase hard-codes a key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from friday.core.config import ConfigError
from friday.core.llm.routing import parse_route

GROUP_ORDER = ("llm", "desktop", "controls")


class SettingValidationError(ValueError):
    """A value does not fit its SettingSpec. The message names the key."""


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str                                  # str | int | float | bool | enum | url | list
    group: str
    description: str
    default: Any = None
    secret: bool = False
    choices: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    env: str | None = None
    restart_required: bool = False
    validator: Callable[[Any], Any] | None = None


def _route(value: Any) -> str:
    route = parse_route(str(value))
    return f"{route.provider}:{route.model}"


def _route_spec(role: str, default: str, *, scopes: tuple[str, ...]) -> SettingSpec:
    return SettingSpec(
        key=f"llm.routes.{role}", type="str", group="llm",
        description=f"provider:model used for the '{role}' role",
        default=default, scopes=scopes, env=f"FRIDAY_LLM_{role.upper()}", validator=_route)


REGISTRY: tuple[SettingSpec, ...] = (
    SettingSpec("llm.gemini_api_key", "str", "llm", "Google Gemini API key",
                secret=True, scopes=("desktop",), env="GEMINI_API_KEY"),
    _route_spec("live", "gemini:gemini-3.1-flash-live-preview", scopes=("desktop",)),
    _route_spec("agent_os", "gemini:gemini-3.8-flash", scopes=("desktop",)),
    _route_spec("agent_spatial", "gemini:gemini-3.8-flash", scopes=("desktop",)),
    _route_spec("widget", "gemini:gemini-3.7-flash", scopes=("desktop",)),
    _route_spec("triage", "gemini:gemini-3.7-flash", scopes=()),
    SettingSpec("llm.tripo_api_key", "str", "llm", "Tripo3D API key (text-to-3D); optional",
                secret=True, scopes=("desktop",), env="TRIPO_API_KEY"),
    SettingSpec("desktop.voice", "str", "desktop", "Gemini Live voice for the desktop (Aoede or Kore)",
                default="Aoede", scopes=("desktop",), env="FRIDAY_VOICE"),
    SettingSpec("controls.call_mode", "enum", "controls",
                "When escalations may place a phone call",
                default="urgent_only", choices=("always", "urgent_only", "mute")),
    SettingSpec("controls.dnd", "bool", "controls", "Do not disturb: suppress calls and pings",
                default=False),
)

_BY_KEY = {spec.key: spec for spec in REGISTRY}


def spec_for(key: str) -> SettingSpec:
    return _BY_KEY[key]


def _fail(spec: SettingSpec, message: str) -> SettingValidationError:
    return SettingValidationError(f"{spec.key}: {message}")


def validate(spec: SettingSpec, value: Any) -> Any:
    """Coerce ``value`` to the spec's type or raise SettingValidationError."""
    kind = spec.type
    if kind in ("str", "url", "enum"):
        if not isinstance(value, str):
            raise _fail(spec, "must be a string")
        value = value.strip()
        if not value:
            raise _fail(spec, "must not be empty")
        if kind == "url" and not (value.startswith("http://") or value.startswith("https://")):
            raise _fail(spec, "must be an http(s) URL")
        if kind == "enum" and value not in spec.choices:
            raise _fail(spec, f"must be one of {', '.join(spec.choices)}")
    elif kind == "bool":
        if isinstance(value, bool):
            pass
        elif isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
            value = True
        elif isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
            value = False
        else:
            raise _fail(spec, "must be true or false")
    elif kind == "int":
        if isinstance(value, bool):
            raise _fail(spec, "must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise _fail(spec, "must be an integer") from None
    elif kind == "float":
        if isinstance(value, bool):
            raise _fail(spec, "must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise _fail(spec, "must be a number") from None
    elif kind == "list":
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = [v.strip() for v in value if v.strip()]
        else:
            raise _fail(spec, "must be a list of strings")
    else:
        raise _fail(spec, f"unknown setting type {kind!r}")
    if spec.validator is not None:
        try:
            value = spec.validator(value)
        except (ConfigError, ValueError) as e:
            raise _fail(spec, str(e)) from e
    return value


def schema() -> list[dict]:
    """JSON-friendly description of the registry, grouped, without values."""
    groups: dict[str, list[dict]] = {name: [] for name in GROUP_ORDER}
    for spec in REGISTRY:
        groups.setdefault(spec.group, []).append({
            "key": spec.key,
            "type": spec.type,
            "description": spec.description,
            "default": spec.default,
            "secret": spec.secret,
            "choices": list(spec.choices),
            "scopes": list(spec.scopes),
            "env": spec.env,
            "restart_required": spec.restart_required,
        })
    return [{"name": name, "keys": keys} for name, keys in groups.items() if keys]
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_settings_registry.py -q`
Expected: all PASS.

---

### Task 5: `RuntimeConfig`

**Files:**
- Create: `friday/sentinel/runtime_config.py`
- Test: `tests/sentinel/test_runtime_config.py`

**Interfaces:**
- Consumes: `AsyncStore` v2 methods (Task 3), `Vault` (Task 2), registry (Task 4), `EventBus.publish`.
- Produces: `legacy_value(spec, settings) -> Any | None`; `class RuntimeConfig(store, vault, settings, bus=None)` with `async load()`, `get(key) -> Any`, `source(key) -> str`, `async set_many(updates, *, actor)`, `async unset(key, *, actor)`, `view_for_user() -> list[dict]`, `view_for_scope(scope) -> dict[str, Any]`, `known_scopes() -> set[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_runtime_config.py`:
```python
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext
from friday.sentinel.runtime_config import RuntimeConfig, legacy_value
from friday.sentinel.settings_registry import SettingValidationError, spec_for


@pytest.fixture
async def rig(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key", GEMINI_MODEL="env-live-model", FRIDAY_LLM_WIDGET="gemini:env-widget")
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store)
    vault = Vault.from_master_key(settings.master_key)
    config = RuntimeConfig(store, vault, settings, bus)
    await config.load()
    yield SimpleNamespace(settings=settings, store=store, bus=bus, vault=vault, config=config)
    await store.aclose()


def test_legacy_value(make_settings):
    s = make_settings(GEMINI_API_KEY="k", GEMINI_MODEL="live-m", FRIDAY_LLM_WIDGET="gemini:w")
    assert legacy_value(spec_for("llm.gemini_api_key"), s) == "k"
    assert legacy_value(spec_for("llm.routes.live"), s) == "gemini:live-m"
    assert legacy_value(spec_for("llm.routes.widget"), s) == "gemini:w"
    assert legacy_value(spec_for("llm.routes.triage"), s) is None
    assert legacy_value(spec_for("controls.dnd"), s) is None
    bad = make_settings(FRIDAY_LLM_TRIAGE="nocolon")
    assert legacy_value(spec_for("llm.routes.triage"), bad) is None      # malformed legacy → ignored


async def test_precedence_vault_env_default(rig):
    c = rig.config
    assert c.get("llm.gemini_api_key") == "env-key" and c.source("llm.gemini_api_key") == "env"
    assert c.get("llm.routes.live") == "gemini:env-live-model" and c.source("llm.routes.live") == "env"
    assert c.get("llm.routes.triage") == "gemini:gemini-3.7-flash" and c.source("llm.routes.triage") == "default"
    assert c.get("controls.dnd") is False and c.source("controls.dnd") == "default"

    await c.set_many({"llm.gemini_api_key": "vault-key", "controls.dnd": "true"}, actor="test")
    assert c.get("llm.gemini_api_key") == "vault-key" and c.source("llm.gemini_api_key") == "vault"
    assert c.get("controls.dnd") is True and c.source("controls.dnd") == "vault"


async def test_secrets_are_encrypted_at_rest_and_never_audited(rig):
    await rig.config.set_many({"llm.gemini_api_key": "hunter2hunter2"}, actor="dashboard:vince")
    row = await rig.store.setting_get("llm.gemini_api_key")
    assert row.value.startswith("v1:") and "hunter2" not in row.value and row.secret is True
    assert rig.vault.decrypt("llm.gemini_api_key", row.value) == "hunter2hunter2"
    audit = await rig.store.audit_list()
    assert audit[0].action == "settings.update" and audit[0].target == "llm.gemini_api_key"
    assert audit[0].actor == "dashboard:vince"
    assert "hunter2" not in str(audit[0].detail) and audit[0].detail["secret"] is True


async def test_non_secret_stored_as_json(rig):
    await rig.config.set_many({"controls.call_mode": "mute"}, actor="test")
    row = await rig.store.setting_get("controls.call_mode")
    assert row.value == '"mute"' and row.secret is False


async def test_set_many_is_all_or_nothing(rig):
    with pytest.raises(SettingValidationError) as excinfo:
        await rig.config.set_many({"controls.dnd": "true", "controls.call_mode": "sometimes", "no.such": 1},
                                  actor="test")
    message = str(excinfo.value)
    assert "controls.call_mode" in message and "no.such" in message
    assert rig.config.source("controls.dnd") == "default"
    assert await rig.store.settings_all() == []


async def test_config_changed_is_published(rig):
    await rig.config.set_many({"controls.dnd": True, "controls.call_mode": "always"}, actor="test")
    rows = await rig.store.list_events(type="config.changed")
    assert len(rows) == 1
    assert sorted(rows[0].event.payload["keys"]) == ["controls.call_mode", "controls.dnd"]
    assert rows[0].event.payload["actor"] == "test"


async def test_unset_returns_to_env_or_default(rig):
    await rig.config.set_many({"llm.gemini_api_key": "vault-key", "controls.dnd": True}, actor="test")
    await rig.config.unset("llm.gemini_api_key", actor="test")
    await rig.config.unset("controls.dnd", actor="test")
    assert rig.config.get("llm.gemini_api_key") == "env-key" and rig.config.source("llm.gemini_api_key") == "env"
    assert rig.config.get("controls.dnd") is False
    await rig.config.unset("controls.dnd", actor="test")          # idempotent


async def test_views(rig):
    await rig.config.set_many({"llm.gemini_api_key": "abcdefghijklmnop", "llm.tripo_api_key": "short"}, actor="t")
    view = {item["key"]: item for item in rig.config.view_for_user()}
    assert view["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": True,
                                          "hint": "mnop", "source": "vault"}
    assert view["llm.tripo_api_key"]["hint"] == ""                    # too short for a hint
    assert view["llm.routes.triage"] == {"key": "llm.routes.triage", "secret": False,
                                         "value": "gemini:gemini-3.7-flash", "source": "default"}
    assert "abcdefgh" not in str(rig.config.view_for_user())

    desktop = rig.config.view_for_scope("desktop")
    assert desktop["llm.gemini_api_key"] == "abcdefghijklmnop"
    assert desktop["llm.routes.widget"] == "gemini:env-widget"
    assert "llm.routes.triage" not in desktop and "controls.dnd" not in desktop
    assert rig.config.known_scopes() == {"desktop"}


async def test_undecryptable_secret_is_treated_as_unset(rig, caplog):
    other = Vault.from_master_key(Vault.generate_master_key())
    await rig.store.setting_set("llm.tripo_api_key", other.encrypt("llm.tripo_api_key", "x"),
                                secret=True, updated_by="old", ts=time.time())
    await rig.config.load()
    with caplog.at_level(logging.WARNING):
        assert rig.config.get("llm.tripo_api_key") is None
        assert rig.config.source("llm.tripo_api_key") == "undecryptable"
        rig.config.get("llm.tripo_api_key")
    assert sum("cannot decrypt" in r.getMessage() for r in caplog.records) == 1
    view = {item["key"]: item for item in rig.config.view_for_user()}
    assert view["llm.tripo_api_key"]["source"] == "undecryptable" and view["llm.tripo_api_key"]["set"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_runtime_config.py -q`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`friday/sentinel/runtime_config.py`:
```python
"""Dynamic configuration: the vault fronted by the registry.

Precedence per key is vault → legacy environment → registry default. Secrets
are decrypted on read and never leave this module except through
``view_for_scope`` (for a node pull) — ``view_for_user`` masks them.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping

from friday.core.config import LLM_ROLES, Settings
from friday.core.events import Event
from friday.core.llm.routing import parse_route
from friday.core.storage import AsyncStore, SettingRow
from friday.core.vault import Vault, VaultError
from friday.sentinel.settings_registry import (REGISTRY, SettingSpec, SettingValidationError,
                                               spec_for, validate)

log = logging.getLogger(__name__)

HINT_MIN_LENGTH = 12
_ROUTE_PREFIX = "llm.routes."


def legacy_value(spec: SettingSpec, settings: Settings) -> Any | None:
    """The value a legacy .env variable supplies for ``spec``, or None."""
    if spec.key.startswith(_ROUTE_PREFIX):
        role = spec.key[len(_ROUTE_PREFIX):]
        if role not in LLM_ROLES:
            return None
        raw = settings.env.get(spec.env or "")
        if not raw and role == "live" and settings.gemini_model:
            raw = f"gemini:{settings.gemini_model}"
        if not raw:
            return None
        try:
            route = parse_route(raw)
        except Exception:
            log.warning("ignoring malformed legacy value for %s: %r", spec.key, raw)
            return None
        return f"{route.provider}:{route.model}"
    if spec.env is None:
        return None
    raw = settings.env.get(spec.env)
    return raw.strip() if raw and raw.strip() else None


class RuntimeConfig:
    def __init__(self, store: AsyncStore, vault: Vault, settings: Settings, bus=None):
        self._store = store
        self._vault = vault
        self._settings = settings
        self._bus = bus
        self._rows: dict[str, SettingRow] = {}
        self._decrypted: dict[str, Any] = {}       # key → value or None (undecryptable)
        self._warned: set[str] = set()

    async def load(self) -> None:
        self._rows = {row.key: row for row in await self._store.settings_all()}
        self._decrypted = {}

    # ------------------------------------------------------------------ read

    def _vault_value(self, spec: SettingSpec) -> tuple[bool, Any]:
        """(present, value). value is None when present but undecryptable."""
        row = self._rows.get(spec.key)
        if row is None:
            return False, None
        if spec.key in self._decrypted:
            return True, self._decrypted[spec.key]
        try:
            text = self._vault.decrypt(spec.key, row.value) if row.secret else row.value
            value = text if spec.secret else json.loads(text)
        except (VaultError, ValueError) as e:
            if spec.key not in self._warned:
                self._warned.add(spec.key)
                log.warning("cannot decrypt setting %s (master key changed?): %s", spec.key, e)
            value = None
        self._decrypted[spec.key] = value
        return True, value

    def source(self, key: str) -> str:
        spec = spec_for(key)
        present, value = self._vault_value(spec)
        if present:
            return "vault" if value is not None else "undecryptable"
        if legacy_value(spec, self._settings) is not None:
            return "env"
        return "default"

    def get(self, key: str) -> Any:
        spec = spec_for(key)
        present, value = self._vault_value(spec)
        if present:
            return value
        legacy = legacy_value(spec, self._settings)
        return legacy if legacy is not None else spec.default

    def known_scopes(self) -> set[str]:
        return {scope for spec in REGISTRY for scope in spec.scopes}

    # ----------------------------------------------------------------- write

    async def set_many(self, updates: Mapping[str, Any], *, actor: str) -> None:
        cleaned: dict[str, tuple[SettingSpec, Any]] = {}
        errors: list[str] = []
        for key, raw in updates.items():
            try:
                spec = spec_for(key)
            except KeyError:
                errors.append(f"{key}: unknown setting")
                continue
            try:
                cleaned[key] = (spec, validate(spec, raw))
            except SettingValidationError as e:
                errors.append(str(e))
        if errors:
            raise SettingValidationError("; ".join(errors))

        now = time.time()
        for key, (spec, value) in cleaned.items():
            stored = self._vault.encrypt(key, str(value)) if spec.secret else json.dumps(value)
            await self._store.setting_set(key, stored, secret=spec.secret, updated_by=actor, ts=now)
            await self._store.audit_append(now, actor, "settings.update", key,
                                           {"secret": True} if spec.secret else {"value": value})
        await self.load()
        await self._publish(sorted(cleaned), actor)

    async def unset(self, key: str, *, actor: str) -> None:
        spec = spec_for(key)
        if await self._store.setting_delete(key):
            now = time.time()
            await self._store.audit_append(now, actor, "settings.unset", key, {"secret": spec.secret})
            await self.load()
            await self._publish([key], actor)

    async def _publish(self, keys: list[str], actor: str) -> None:
        if self._bus is None:
            return
        await self._bus.publish(Event(type="config.changed", source=self._settings.node_id,
                                      payload={"keys": keys, "actor": actor}))

    # ----------------------------------------------------------------- views

    def view_for_user(self) -> list[dict]:
        out: list[dict] = []
        for spec in REGISTRY:
            source = self.source(spec.key)
            if spec.secret:
                value = self.get(spec.key)
                text = "" if value is None else str(value)
                out.append({"key": spec.key, "secret": True, "set": bool(text),
                            "hint": text[-4:] if len(text) >= HINT_MIN_LENGTH else "",
                            "source": source})
            else:
                out.append({"key": spec.key, "secret": False, "value": self.get(spec.key),
                            "source": source})
        return out

    def view_for_scope(self, scope: str) -> dict[str, Any]:
        return {spec.key: self.get(spec.key) for spec in REGISTRY if scope in spec.scopes}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_runtime_config.py -q`
Expected: all PASS.

---

### Task 6: `friday.sentinel.auth`

**Files:**
- Create: `friday/sentinel/auth.py`
- Test: `tests/sentinel/test_auth.py`

**Interfaces:**
- Produces: `hash_password(password) -> str`, `verify_password(password, stored) -> bool`, `new_token(prefix="") -> str`, `token_hash(token) -> str`, `@dataclass(frozen=True) User(username)`, `@dataclass(frozen=True) Node(id, name)`, `Principal = User | Node`, `class SessionManager(store, ttl_s=2592000.0)` with `async create(username, *, user_agent, ip) -> str`, `async resolve(token) -> User | None`, `async revoke(token)`, `ttl_s`; `class NodeTokens(store)` with `async create(name) -> tuple[str, str]`, `async resolve(token) -> Node | None`, `async revoke(id) -> bool`, `async list() -> list[NodeTokenRow]`; `class LoginLimiter(max_failures=5, lockout_s=60.0)` with `allowed(ip) -> bool`, `retry_after(ip) -> int`, `record_failure(ip)`, `reset(ip)`; `client_ip(request, trusted_proxy) -> str`; `is_https(request, trusted_proxy) -> bool`; `SESSION_COOKIE = "friday_session"`, `NODE_TOKEN_PREFIX = "fn_"`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_auth.py`:
```python
import time
from types import SimpleNamespace

import pytest

from friday.core.storage import AsyncStore
from friday.sentinel.auth import (NODE_TOKEN_PREFIX, LoginLimiter, Node, NodeTokens, SessionManager, User,
                                  client_ip, hash_password, is_https, new_token, token_hash, verify_password)


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$32768$8$1$")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong", stored) is False
    assert hash_password("correct horse") != stored                  # fresh salt


def test_verify_tolerates_garbage():
    assert verify_password("x", "") is False
    assert verify_password("x", "bcrypt$nope") is False
    assert verify_password("x", "scrypt$a$b$c$d$e") is False


def test_tokens():
    t = new_token("fn_")
    assert t.startswith("fn_") and len(t) > 40 and new_token("fn_") != t
    assert token_hash(t) == token_hash(t) and len(token_hash(t)) == 64
    assert NODE_TOKEN_PREFIX == "fn_"


@pytest.fixture
async def store(tmp_path):
    s = await AsyncStore.open(tmp_path / "t.db")
    yield s
    await s.aclose()


async def test_sessions(store):
    sessions = SessionManager(store, ttl_s=100.0)
    token = await sessions.create("vince", user_agent="ua", ip="1.1.1.1")
    assert await sessions.resolve(token) == User("vince")
    assert await sessions.resolve("nope") is None
    assert (await store.session_get(token_hash(token))).user_agent == "ua"
    await sessions.revoke(token)
    assert await sessions.resolve(token) is None
    await sessions.revoke(token)                                         # idempotent


async def test_session_expiry(store):
    sessions = SessionManager(store, ttl_s=0.01)
    token = await sessions.create("vince", user_agent=None, ip=None)
    time.sleep(0.02)
    assert await sessions.resolve(token) is None
    assert await store.session_get(token_hash(token)) is None            # expired row removed on touch


async def test_node_tokens(store):
    tokens = NodeTokens(store)
    id_, plaintext = await tokens.create("desktop")
    assert plaintext.startswith("fn_")
    assert await tokens.resolve(plaintext) == Node(id_, "desktop")
    assert await tokens.resolve("fn_nope") is None
    listed = await tokens.list()
    assert [t.name for t in listed] == ["desktop"] and listed[0].last_used is not None
    assert await tokens.revoke(id_) is True
    assert await tokens.resolve(plaintext) is None
    assert await tokens.revoke(id_) is False


def test_login_limiter(monkeypatch):
    now = [1000.0]
    limiter = LoginLimiter(max_failures=3, lockout_s=60.0, clock=lambda: now[0])
    ip = "9.9.9.9"
    assert limiter.allowed(ip)
    for _ in range(3):
        limiter.record_failure(ip)
    assert not limiter.allowed(ip) and limiter.retry_after(ip) == 60
    now[0] += 30
    assert limiter.retry_after(ip) == 30
    now[0] += 31
    assert limiter.allowed(ip)
    limiter.record_failure(ip)
    limiter.reset(ip)
    assert limiter.allowed(ip) and limiter.retry_after(ip) == 0
    assert limiter.allowed("other")


def _request(headers=None, remote="10.0.0.5", secure=False):
    return SimpleNamespace(headers=headers or {}, remote=remote, secure=secure)


def test_client_ip_and_https():
    r = _request({"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "X-Forwarded-Proto": "https"})
    assert client_ip(r, trusted_proxy=False) == "10.0.0.5"
    assert client_ip(r, trusted_proxy=True) == "203.0.113.9"
    assert is_https(r, trusted_proxy=False) is False
    assert is_https(r, trusted_proxy=True) is True
    assert is_https(_request(secure=True), trusted_proxy=False) is True
    assert client_ip(_request(remote=None), trusted_proxy=False) == "unknown"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_auth.py -q`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`friday/sentinel/auth.py`:
```python
"""Passwords, sessions, node tokens and login throttling. Pure of aiohttp
routing — request handling lives in principals.py / web.py."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from friday.core.storage import AsyncStore, NodeTokenRow

SESSION_COOKIE = "friday_session"
NODE_TOKEN_PREFIX = "fn_"
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 15, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024


@dataclass(frozen=True)
class User:
    username: str


@dataclass(frozen=True)
class Node:
    id: str
    name: str


Principal = User | Node


# -------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ----------------------------------------------------------------- tokens

def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionManager:
    def __init__(self, store: AsyncStore, ttl_s: float = 30 * 86400.0):
        self._store = store
        self.ttl_s = ttl_s

    async def create(self, username: str, *, user_agent: str | None, ip: str | None) -> str:
        token = new_token()
        now = time.time()
        await self._store.session_create(token_hash(token), username, now, now + self.ttl_s,
                                         user_agent, ip)
        return token

    async def resolve(self, token: str) -> User | None:
        row = await self._store.session_get(token_hash(token))
        if row is None:
            return None
        now = time.time()
        if row.expires_at <= now:
            await self._store.session_delete(row.id)
            return None
        await self._store.session_touch(row.id, now)
        return User(row.username)

    async def revoke(self, token: str) -> None:
        await self._store.session_delete(token_hash(token))


class NodeTokens:
    def __init__(self, store: AsyncStore):
        self._store = store

    async def create(self, name: str) -> tuple[str, str]:
        token = new_token(NODE_TOKEN_PREFIX)
        id_ = secrets.token_hex(8)
        await self._store.node_token_create(id_, name, token_hash(token), time.time())
        return id_, token

    async def resolve(self, token: str) -> Node | None:
        row = await self._store.node_token_by_hash(token_hash(token))
        if row is None:
            return None
        await self._store.node_token_touch(row.id, time.time())
        return Node(row.id, row.name)

    async def revoke(self, id_: str) -> bool:
        return await self._store.node_token_revoke(id_, time.time())

    async def list(self) -> list[NodeTokenRow]:
        return await self._store.node_tokens_list()


class LoginLimiter:
    """Per-IP lockout after repeated failures. In memory: a restart forgives."""

    def __init__(self, max_failures: int = 5, lockout_s: float = 60.0,
                 clock: Callable[[], float] = time.time):
        self._max = max_failures
        self._lockout_s = lockout_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str) -> int:
        until = self._locked_until.get(ip, 0.0)
        remaining = until - self._clock()
        if remaining <= 0:
            self._locked_until.pop(ip, None)
            return 0
        return int(remaining + 0.999)

    def allowed(self, ip: str) -> bool:
        return self.retry_after(ip) == 0

    def record_failure(self, ip: str) -> None:
        count = self._failures.get(ip, 0) + 1
        self._failures[ip] = count
        if count >= self._max:
            self._locked_until[ip] = self._clock() + self._lockout_s
            self._failures[ip] = 0

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)


# --------------------------------------------------------------- requests

def client_ip(request, trusted_proxy: bool) -> str:
    if trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def is_https(request, trusted_proxy: bool) -> bool:
    if getattr(request, "secure", False):
        return True
    return trusted_proxy and request.headers.get("X-Forwarded-Proto", "").lower() == "https"
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_auth.py -q`
Expected: all PASS (the scrypt tests take ~0.1 s each).

---

### Task 7: `Services`, principals, node API on principals

**Files:**
- Create: `friday/sentinel/services.py`, `friday/sentinel/principals.py`, `tests/sentinel/conftest.py`
- Modify: `friday/sentinel/api.py`, `tests/sentinel/test_api.py`

**Interfaces:**
- Produces `services.py`: `@dataclass HealthState(started_at, platform, restarts={})` (moved here), `@dataclass Services(settings, store, bus, state, vault, config, sessions, node_tokens, limiter)`, `SERVICES = web.AppKey("services", Services)`.
- Produces `principals.py`: `async resolve_principal(request, *, allow_query=False) -> Principal | None`, `def unauthorized() -> web.Response` (401 JSON), `async require_principal(request, *, allow_query=False) -> Principal` (raises `web.HTTPUnauthorized` with the JSON body), `async require_user(request) -> User`.
- Changes `api.py`: `HealthState` re-exported from `services`; `create_app(services: Services, *, static_dir: Path | None = None) -> web.Application`; `ApiServer(services, *, static_dir=None)`; `/health` open; `/nodes`, `/telemetry`, `/events` require a principal; `/ws` requires a principal (cookie, bearer, or `?token=`); `_authorized` removed. `create_app` calls `web_routes.add_web_routes(app, static_dir)` **only after Task 8 exists** — in this task it does not yet call it (Task 8 adds the call).
- Produces test fixtures in `tests/sentinel/conftest.py`: `build_services(make_settings, tmp_path, **env) -> Services` (async; starts a dispatcher task stored on `services._dispatcher`), `teardown_services(services)`, fixture `services`, fixture `node_token(services) -> str`, helper `node_headers(token) -> dict`.

- [ ] **Step 1: Write the shared test rig**

`tests/sentinel/conftest.py`:
```python
"""Sentinel test rig: a fully wired Services with a running dispatcher."""

import asyncio
import logging
import time

import pytest

from friday.core.platform import detect
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext, HeartbeatHandler, TelemetryHandler
from friday.sentinel.runtime_config import RuntimeConfig
from friday.sentinel.services import HealthState, Services


async def build_services(make_settings, tmp_path, **env) -> Services:
    env.setdefault("FRIDAY_NODE_ID", "sentinel-test")
    settings = make_settings(**env)
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store, poll_interval_s=0.05)
    bus.subscribe(HeartbeatHandler())
    bus.subscribe(TelemetryHandler())
    vault = Vault.from_master_key(settings.master_key)
    config = RuntimeConfig(store, vault, settings, bus)
    await config.load()
    services = Services(
        settings=settings, store=store, bus=bus,
        state=HealthState(started_at=time.time(), platform=detect(), restarts={"x": 2}),
        vault=vault, config=config,
        sessions=SessionManager(store), node_tokens=NodeTokens(store),
        limiter=LoginLimiter(max_failures=3, lockout_s=60.0),
    )
    ctx = HandlerContext(settings=settings, store=store, bus=bus, logger=logging.getLogger("test"))
    services._dispatcher = asyncio.create_task(bus.run_dispatcher(ctx))   # test-only attribute
    return services


async def teardown_services(services: Services) -> None:
    await services.bus.drain()
    await asyncio.wait_for(services._dispatcher, 2)
    await services.store.aclose()


@pytest.fixture
async def services(make_settings, tmp_path):
    s = await build_services(make_settings, tmp_path)
    yield s
    await teardown_services(s)


@pytest.fixture
async def node_token(services) -> str:
    _, token = await services.node_tokens.create("test-node")
    return token


def node_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")
```

- [ ] **Step 2: Rewrite `tests/sentinel/test_api.py` against principals**

Replace the file's fixtures and auth tests; keep the behavioural tests. Full new file:
```python
import asyncio
import time

import aiohttp
import pytest

import friday
from friday.core.events import Event, Heartbeat
from friday.sentinel.api import MAX_BODY_BYTES, ApiServer, HealthState, create_app
from tests.sentinel.conftest import build_services, node_headers, teardown_services, until


@pytest.fixture
async def client(aiohttp_client, services):
    return await aiohttp_client(create_app(services))


async def test_health_is_open(client):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["node_id"] == "sentinel-test" and body["version"] == friday.__version__
    assert body["queue"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    assert body["supervisor_restarts"] == {"x": 2}


async def test_node_routes_require_a_principal(client):
    assert (await client.get("/nodes")).status == 401
    assert (await client.get("/telemetry")).status == 401
    assert (await client.post("/events", json={"type": "a.b", "source": "s"})).status == 401
    assert (await client.get("/ws")).status == 401
    assert (await client.post("/events", json={"type": "a.b", "source": "s"},
                              headers=node_headers("fn_wrong"))).status == 401
    assert (await (await client.get("/nodes")).json()) == {"error": "authentication required"}


async def test_telemetry_204_then_200(client, services, node_token):
    assert (await client.get("/telemetry", headers=node_headers(node_token))).status == 204
    await services.store.telemetry_insert("sentinel-test", {"cpu_percent": 1.5}, ts=1.0)
    resp = await client.get("/telemetry", headers=node_headers(node_token))
    assert resp.status == 200 and (await resp.json())["cpu_percent"] == 1.5


async def test_post_single_event_and_nodes(client, services, node_token):
    hb = Heartbeat(status="listening", version="0.1.0", platform="Darwin/arm64")
    resp = await client.post("/events", json={"type": "node.heartbeat", "source": "mac", "payload": hb.to_dict()},
                             headers=node_headers(node_token))
    assert resp.status == 202
    ids = (await resp.json())["ids"]
    assert len(ids) == 1 and len(ids[0]) == 32

    async def done():
        return (await services.store.queue_depths())["done"] == 1
    await until(done)
    nodes = await (await client.get("/nodes", headers=node_headers(node_token))).json()
    assert nodes[0]["node_id"] == "mac" and nodes[0]["status"] == "listening"


async def test_revoked_token_is_401(client, services, node_token):
    listed = await services.node_tokens.list()
    await services.node_tokens.revoke(listed[0].id)
    assert (await client.get("/nodes", headers=node_headers(node_token))).status == 401


async def test_post_batch_and_rejections(client, services, node_token):
    h = node_headers(node_token)
    ok = await client.post("/events", json=[{"type": "a.b", "source": "s"}, {"type": "a.c", "source": "s"}], headers=h)
    assert ok.status == 202 and len((await ok.json())["ids"]) == 2
    bad = await client.post("/events", json=[{"type": "a.b", "source": "s"}, {"type": "BAD", "source": "s"}], headers=h)
    assert bad.status == 400 and "event 1" in (await bad.json())["error"]
    assert (await client.post("/events", json=[{"type": "a.b", "source": "s"}] * 101, headers=h)).status == 400
    for body in (b"{not json", b"[]", b'"just a string"', b"[1, 2]"):
        resp = await client.post("/events", data=body, headers={**h, "Content-Type": "application/json"})
        assert resp.status == 400
    big = {"type": "a.b", "source": "s", "payload": {"blob": "x" * (MAX_BODY_BYTES + 1024)}}
    assert (await client.post("/events", json=big, headers=h)).status == 413


async def test_ws_bearer_query_and_filtering(client, services, node_token):
    ws = await client.ws_connect(f"/ws?token={node_token}")
    await ws.send_json({"subscribe": ["node.*"]})
    assert await asyncio.wait_for(ws.receive_json(), 2) == {"subscribed": ["node.*"]}
    await services.bus.publish(Event(type="telemetry.sample", source="s", payload={}))
    await services.bus.publish(Event(type="node.heartbeat", source="mac",
                                     payload=Heartbeat("idle", "0.1.0", "x").to_dict()))
    msg = await asyncio.wait_for(ws.receive_json(), 2)
    assert msg["type"] == "node.heartbeat"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive_json(), 0.3)
    await ws.close()

    ws2 = await client.ws_connect("/ws", headers=node_headers(node_token))
    await services.bus.publish(Event(type="anything.goes", source="s"))
    assert (await asyncio.wait_for(ws2.receive_json(), 2))["type"] == "anything.goes"
    await ws2.send_str("not json")
    assert "error" in await asyncio.wait_for(ws2.receive_json(), 2)
    await ws2.close()


async def test_dead_socket_is_dropped_without_failing_the_event(client, services, node_token):
    ws = await client.ws_connect("/ws", headers=node_headers(node_token))
    await ws.close()
    await asyncio.sleep(0.05)
    await services.bus.publish(Event(type="a.b", source="s"))

    async def done():
        return (await services.store.queue_depths())["done"] == 1
    await until(done)
    assert (await services.store.queue_depths())["failed"] == 0


async def test_server_start_stop_on_ephemeral_port(make_settings, tmp_path):
    services = await build_services(make_settings, tmp_path, FRIDAY_SENTINEL_BIND="127.0.0.1:0")
    server = ApiServer(services)
    await server.start()
    try:
        assert server.port and server.port > 0
        assert services.bus.handlers_for("x.y") == [server.fanout]
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{server.port}/health") as resp:
                assert resp.status == 200
    finally:
        await server.stop()
        await teardown_services(services)
    assert services.bus.handlers_for("x.y") == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_api.py -q`
Expected: import errors (`friday.sentinel.services`), then signature mismatches.

- [ ] **Step 4: Implement `services.py` and `principals.py`**

`friday/sentinel/services.py`:
```python
"""Everything a request handler or monitor may need, in one object."""

from __future__ import annotations

from dataclasses import dataclass, field

from aiohttp import web

from friday.core.config import Settings
from friday.core.platform import PlatformInfo
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.bus import EventBus
from friday.sentinel.runtime_config import RuntimeConfig


@dataclass
class HealthState:
    started_at: float
    platform: PlatformInfo
    restarts: dict[str, int] = field(default_factory=dict)


@dataclass
class Services:
    settings: Settings
    store: AsyncStore
    bus: EventBus
    state: HealthState
    vault: Vault
    config: RuntimeConfig
    sessions: SessionManager
    node_tokens: NodeTokens
    limiter: LoginLimiter


SERVICES = web.AppKey("services", Services)
```

`friday/sentinel/principals.py`:
```python
"""Who is asking? A dashboard user (session cookie) or a node (bearer token)."""

from __future__ import annotations

from aiohttp import web

from friday.sentinel.auth import SESSION_COOKIE, Principal, User
from friday.sentinel.services import SERVICES


def unauthorized() -> web.Response:
    return web.json_response({"error": "authentication required"}, status=401)


def _raise_unauthorized() -> web.HTTPUnauthorized:
    return web.HTTPUnauthorized(text='{"error": "authentication required"}',
                                content_type="application/json")


async def resolve_principal(request: web.Request, *, allow_query: bool = False) -> Principal | None:
    services = request.app[SERVICES]
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        user = await services.sessions.resolve(cookie)
        if user is not None:
            return user
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token and allow_query:
        token = request.query.get("token", "")
    if token:
        return await services.node_tokens.resolve(token)
    return None


async def require_principal(request: web.Request, *, allow_query: bool = False) -> Principal:
    principal = await resolve_principal(request, allow_query=allow_query)
    if principal is None:
        raise _raise_unauthorized()
    return principal


async def require_user(request: web.Request) -> User:
    principal = await resolve_principal(request)
    if not isinstance(principal, User):
        raise _raise_unauthorized()
    return principal
```

- [ ] **Step 5: Rework `api.py` onto `Services` and principals**

In `friday/sentinel/api.py`:

Replace the imports of `Settings`, `PlatformInfo`, `AsyncStore`, `EventBus` usage and the `HealthState` definition with:
```python
from pathlib import Path

from friday.sentinel.principals import require_principal
from friday.sentinel.services import SERVICES, HealthState, Services   # noqa: F401  (HealthState re-exported)
```
Delete the `HealthState` dataclass, the five `web.AppKey` lines except `FANOUT`, `_authorized`, and the `hmac` import.

Rewrite the handlers to read from `request.app[SERVICES]`:
```python
async def health(request: web.Request) -> web.Response:
    svc = request.app[SERVICES]
    return web.json_response({
        "status": "ok",
        "node_id": svc.settings.node_id,
        "version": friday.__version__,
        "uptime_s": max(0.0, time.time() - svc.state.started_at),
        "platform": svc.state.platform.to_dict(),
        "queue": await svc.store.queue_depths(),
        "supervisor_restarts": dict(svc.state.restarts),
    })


async def telemetry(request: web.Request) -> web.Response:
    await require_principal(request)
    svc = request.app[SERVICES]
    snapshot = await svc.store.telemetry_latest(svc.settings.node_id)
    return web.Response(status=204) if snapshot is None else web.json_response(snapshot)


async def nodes(request: web.Request) -> web.Response:
    await require_principal(request)
    rows = await request.app[SERVICES].store.heartbeats()
    return web.json_response([{"node_id": r.node_id, "last_seen": r.last_seen,
                               "status": r.status, "meta": r.meta} for r in rows])
```
In `post_events` replace the `_authorized` check with `await require_principal(request)` and `app[BUS]` with `request.app[SERVICES].bus`. In `websocket` replace the check with `await require_principal(request, allow_query=True)`.

Replace `create_app` and `ApiServer`:
```python
def create_app(services: Services, *, static_dir: Path | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    fanout = WebSocketFanout()
    services.bus.subscribe(fanout)
    app[SERVICES] = services
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
    def __init__(self, services: Services, *, static_dir: Path | None = None):
        self._services = services
        self.app = create_app(services, static_dir=static_dir)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def fanout(self) -> WebSocketFanout:
        return self.app[FANOUT]

    async def start(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._services.settings.sentinel_bind_host,
                           self._services.settings.sentinel_bind_port)
        await site.start()
        self._runner = runner
        addresses = runner.addresses
        self.port = addresses[0][1] if addresses else self._services.settings.sentinel_bind_port

    async def stop(self) -> None:
        self._services.bus.unsubscribe(self.fanout)
        await self.fanout.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
```
(`static_dir` is accepted now and used by Task 8.)

Because `web.HTTPUnauthorized` raised inside a handler becomes the response, the JSON body set in `_raise_unauthorized` is what clients see.

- [ ] **Step 6: Run to verify**

Run: `.venv/bin/pytest tests/sentinel/test_api.py tests/sentinel/test_handlers.py tests/sentinel/test_bus.py tests/sentinel/test_monitors.py -q`
Expected: all PASS. (`test_daemon.py` breaks until Task 10 — expected.)

---

### Task 8: `friday.sentinel.web` — auth, settings, tokens, audit, config pull, shell + static

**Files:**
- Create: `friday/sentinel/web.py`
- Modify: `friday/sentinel/api.py` (call `add_web_routes` from `create_app`), `friday/sentinel/settings_registry.py` and `friday/sentinel/runtime_config.py` (structured `errors` on `SettingValidationError`)
- Test: `tests/sentinel/test_web.py`

**Interfaces:**
- Changes: `SettingValidationError(message, errors: dict[str, str] | None = None)` with `.errors`; `validate()` raises with `errors={spec.key: message}`; `RuntimeConfig.set_many` raises one error whose `.errors` maps every bad key.
- Produces `web.py`: `CLIENT_HEADER = "X-FRIDAY-Client"`, `DASHBOARD_DIR = Path(__file__).parent / "dashboard"`, `STATIC_DIR = web.AppKey(...)`, `add_web_routes(app: web.Application, static_dir: Path | None) -> None`, routes per the spec §8.
- `create_app(services, *, static_dir=None)` now calls `add_web_routes(app, static_dir)`.

- [ ] **Step 1: Structured validation errors (small prerequisite)**

In `friday/sentinel/settings_registry.py` replace the exception class:
```python
class SettingValidationError(ValueError):
    """A value does not fit its SettingSpec. ``errors`` maps key → message."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors = errors or {}
```
and `_fail`:
```python
def _fail(spec: SettingSpec, message: str) -> SettingValidationError:
    return SettingValidationError(f"{spec.key}: {message}", {spec.key: message})
```

In `friday/sentinel/runtime_config.py`, `set_many` collects a dict:
```python
        cleaned: dict[str, tuple[SettingSpec, Any]] = {}
        errors: dict[str, str] = {}
        for key, raw in updates.items():
            try:
                spec = spec_for(key)
            except KeyError:
                errors[key] = "unknown setting"
                continue
            try:
                cleaned[key] = (spec, validate(spec, raw))
            except SettingValidationError as e:
                errors[key] = e.errors.get(key, str(e))
        if errors:
            raise SettingValidationError(
                "; ".join(f"{k}: {v}" for k, v in errors.items()), errors)
```
Run: `.venv/bin/pytest tests/sentinel/test_settings_registry.py tests/sentinel/test_runtime_config.py -q` → all PASS (messages unchanged).

- [ ] **Step 2: Write the failing tests**

`tests/sentinel/test_web.py`:
```python
import asyncio
import time

import pytest

from friday.core.events import Event
from friday.sentinel.api import create_app
from friday.sentinel.auth import SESSION_COOKIE, hash_password
from tests.sentinel.conftest import build_services, node_headers, teardown_services

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "dash"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>FRIDAY</title><script src=\"static/app.js\"></script>")
    (d / "app.js").write_text("console.log('hi')")
    return d


@pytest.fixture
async def client(aiohttp_client, services, static_dir):
    await services.store.user_upsert("vince", hash_password("pw-correct"), ts=time.time())
    return await aiohttp_client(create_app(services, static_dir=static_dir))


async def login(client, password="pw-correct"):
    return await client.post("/auth/login", json={"username": "vince", "password": password}, headers=CSRF)


# ------------------------------------------------------------------ auth

async def test_login_requires_csrf_header(client):
    resp = await client.post("/auth/login", json={"username": "vince", "password": "pw-correct"})
    assert resp.status == 403 and "client header" in (await resp.json())["error"]


async def test_login_wrong_password_is_401_and_audited(client, services):
    assert (await login(client, "nope")).status == 401
    assert (await login(client, "")).status == 401
    resp = await client.post("/auth/login", json={"username": "ghost", "password": "x"}, headers=CSRF)
    assert resp.status == 401
    rows = await services.store.audit_list()
    assert [r.action for r in rows][:3] == ["login.failed"] * 3
    assert rows[0].target == "ghost" and rows[0].actor.startswith("ip:")


async def test_login_ok_sets_hardened_cookie(client, services):
    resp = await login(client)
    assert resp.status == 204
    cookie = resp.cookies[SESSION_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie["path"] == "/"
    assert int(cookie["max-age"]) == int(services.sessions.ttl_s)
    assert not cookie["secure"]
    me = await client.get("/auth/me")
    assert me.status == 200 and (await me.json())["username"] == "vince"
    assert (await me.json())["expires_at"] > time.time()
    assert (await services.store.audit_list())[0].action == "login.ok"


async def test_secure_flag_follows_forwarded_proto_only_when_trusted(aiohttp_client, make_settings, tmp_path, static_dir):
    for trusted, expected in (("false", False), ("true", True)):
        services = await build_services(make_settings, tmp_path / trusted, FRIDAY_TRUSTED_PROXY=trusted)
        await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
        client = await aiohttp_client(create_app(services, static_dir=static_dir))
        try:
            resp = await client.post("/auth/login", json={"username": "vince", "password": "pw"},
                                     headers={**CSRF, "X-Forwarded-Proto": "https"})
            assert resp.status == 204
            assert bool(resp.cookies[SESSION_COOKIE]["secure"]) is expected
        finally:
            await client.close()
            await teardown_services(services)


async def test_lockout_after_repeated_failures(client, services):
    for _ in range(3):
        assert (await login(client, "bad")).status == 401
    resp = await login(client)
    assert resp.status == 429
    body = await resp.json()
    assert body["error"] == "too many attempts" and 0 < body["retry_after"] <= 60


async def test_me_and_api_require_session(client):
    assert (await client.get("/auth/me")).status == 401
    assert (await client.get("/api/settings")).status == 401
    assert (await client.get("/api/settings/schema")).status == 401
    assert (await client.get("/api/tokens")).status == 401
    assert (await client.get("/api/audit")).status == 401


async def test_logout_clears_session(client, services):
    await login(client)
    resp = await client.post("/auth/logout", headers=CSRF)
    assert resp.status == 204
    assert (await client.get("/auth/me")).status == 401
    assert (await services.store.audit_list())[0].action == "logout"


async def test_state_changing_api_requires_csrf_header(client):
    await login(client)
    assert (await client.put("/api/settings", json={"controls.dnd": True})).status == 403
    assert (await client.post("/api/tokens", json={"name": "x"})).status == 403
    assert (await client.post("/auth/logout")).status == 403


async def test_origin_must_match_host(client):
    await login(client)
    resp = await client.put("/api/settings", json={"controls.dnd": True},
                            headers={**CSRF, "Origin": "https://evil.example"})
    assert resp.status == 403
    resp = await client.put("/api/settings", json={"controls.dnd": True},
                            headers={**CSRF, "Origin": f"http://{client.host}:{client.port}"})
    assert resp.status == 204


# -------------------------------------------------------------- settings

async def test_settings_schema_get_put(client, services):
    await login(client)
    schema = await (await client.get("/api/settings/schema")).json()
    assert [g["name"] for g in schema["groups"]] == ["llm", "desktop", "controls"]

    values = {v["key"]: v for v in (await (await client.get("/api/settings")).json())["values"]}
    assert values["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": False,
                                            "hint": "", "source": "default"}
    assert values["controls.call_mode"]["value"] == "urgent_only"

    resp = await client.put("/api/settings", json={"llm.gemini_api_key": "sk-1234567890abcdef",
                                                   "controls.call_mode": "mute"}, headers=CSRF)
    assert resp.status == 204
    values = {v["key"]: v for v in (await (await client.get("/api/settings")).json())["values"]}
    assert values["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": True,
                                            "hint": "cdef", "source": "vault"}
    assert values["controls.call_mode"]["value"] == "mute"
    assert services.config.get("llm.gemini_api_key") == "sk-1234567890abcdef"
    assert "sk-1234567890abcdef" not in str(await (await client.get("/api/settings")).json())
    actions = [(r.action, r.target, r.actor) for r in await services.store.audit_list(limit=2)]
    assert ("settings.update", "llm.gemini_api_key", "user:vince") in actions

    resp = await client.put("/api/settings", json={"controls.call_mode": "loud", "no.such": 1,
                                                   "controls.dnd": True}, headers=CSRF)
    assert resp.status == 400
    body = await resp.json()
    assert set(body["invalid"]) == {"controls.call_mode", "no.such"}
    assert services.config.source("controls.dnd") == "default"          # nothing written

    resp = await client.put("/api/settings", json={"controls.call_mode": None}, headers=CSRF)
    assert resp.status == 204 and services.config.source("controls.call_mode") == "default"
    resp = await client.put("/api/settings", json={"no.such": None}, headers=CSRF)
    assert resp.status == 400
    resp = await client.put("/api/settings", data=b"[]", headers={**CSRF, "Content-Type": "application/json"})
    assert resp.status == 400


# ---------------------------------------------------------------- tokens

async def test_tokens_lifecycle(client, services):
    await login(client)
    assert await (await client.get("/api/tokens")).json() == []
    resp = await client.post("/api/tokens", json={"name": "desktop"}, headers=CSRF)
    assert resp.status == 201
    created = await resp.json()
    assert created["name"] == "desktop" and created["token"].startswith("fn_")
    assert (await client.get("/nodes", headers=node_headers(created["token"]))).status == 200

    listed = await (await client.get("/api/tokens")).json()
    assert listed[0]["name"] == "desktop" and "token" not in listed[0] and listed[0]["revoked_at"] is None
    assert (await client.delete(f"/api/tokens/{created['id']}", headers=CSRF)).status == 204
    assert (await client.delete(f"/api/tokens/{created['id']}", headers=CSRF)).status == 404
    assert (await client.get("/nodes", headers=node_headers(created["token"]))).status == 401
    actions = [r.action for r in await services.store.audit_list(limit=3)]
    assert "token.create" in actions and "token.revoke" in actions
    assert (await client.post("/api/tokens", json={"name": ""}, headers=CSRF)).status == 400
    assert (await client.post("/api/tokens", json={"name": "x" * 65}, headers=CSRF)).status == 400


# ----------------------------------------------------------------- audit

async def test_audit_listing_and_paging(client, services):
    await login(client)
    for i in range(5):
        await services.store.audit_append(float(i), "test", "noop", str(i), {})
    page = await (await client.get("/api/audit?limit=3")).json()
    assert len(page) == 3 and page[0]["action"] == "noop" and page[0]["target"] == "4"
    older = await (await client.get(f"/api/audit?limit=10&before={page[-1]['id']}")).json()
    assert [r["target"] for r in older if r["action"] == "noop"] == ["1", "0"]
    assert (await client.get("/api/audit?limit=0")).status == 400
    assert (await client.get("/api/audit?before=x")).status == 400


# ---------------------------------------------------------------- config

async def test_config_pull(client, services, node_token):
    await services.config.set_many({"llm.gemini_api_key": "sk-abcdefghijklmnop"}, actor="test")
    resp = await client.get("/config?scope=desktop", headers=node_headers(node_token))
    assert resp.status == 200
    body = await resp.json()
    assert body["scope"] == "desktop" and body["values"]["llm.gemini_api_key"] == "sk-abcdefghijklmnop"
    assert "llm.routes.triage" not in body["values"] and body["generated_at"] > 0
    audit = (await services.store.audit_list())[0]
    assert audit.action == "config.pull" and audit.actor == "node:test-node" and audit.detail["scope"] == "desktop"

    assert (await client.get("/config?scope=desktop")).status == 401
    assert (await client.get("/config?scope=phone", headers=node_headers(node_token))).status == 400
    assert (await client.get("/config", headers=node_headers(node_token))).status == 400
    await login(client)
    assert (await client.get("/config?scope=desktop")).status == 200


# ----------------------------------------------------------------- shell

async def test_shell_and_static(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "login"
    resp = await client.get("/login")
    assert resp.status == 200 and "static/app.js" in await resp.text()
    resp = await client.get("/static/app.js")
    assert resp.status == 200 and "console.log" in await resp.text()
    await login(client)
    for path in ("/", "/settings", "/tokens"):
        resp = await client.get(path, allow_redirects=False)
        assert resp.status == 200 and "FRIDAY" in await resp.text()


async def test_ws_accepts_session_cookie(client, services):
    await login(client)
    ws = await client.ws_connect("/ws")
    await services.bus.publish(Event(type="a.b", source="s"))
    assert (await asyncio.wait_for(ws.receive_json(), 2))["type"] == "a.b"
    await ws.close()
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_web.py -q`
Expected: 404s / attribute errors — the routes do not exist.

- [ ] **Step 4: Implement `web.py`**

`friday/sentinel/web.py`:
```python
"""Dashboard-facing routes: login, settings, node tokens, audit, config pull,
the SPA shell and its static files.

CSRF posture: the session cookie is SameSite=Lax and every state-changing
request must carry ``X-FRIDAY-Client: dashboard`` — a cross-origin page
cannot add a custom header without a CORS preflight, and no CORS headers are
served. Origin is checked against Host as a second layer.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from friday.sentinel.auth import (SESSION_COOKIE, Node, User, client_ip, hash_password, is_https,
                                  token_hash, verify_password)
from friday.sentinel.principals import require_principal, require_user, resolve_principal
from friday.sentinel.services import SERVICES
from friday.sentinel.settings_registry import SettingValidationError, schema, spec_for

CLIENT_HEADER = "X-FRIDAY-Client"
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
STATIC_DIR = web.AppKey("static_dir", Path)
MAX_TOKEN_NAME = 64
_dummy_hash: str | None = None


def _error(status: int, message: str, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _expected_host(request: web.Request) -> str:
    settings = request.app[SERVICES].settings
    if settings.trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.host


def _csrf_check(request: web.Request) -> web.Response | None:
    if request.headers.get(CLIENT_HEADER) != "dashboard":
        return _error(403, "missing client header")
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != _expected_host(request):
        return _error(403, "origin mismatch")
    return None


async def _verify(password: str, stored: str | None) -> bool:
    """Constant-work verification: unknown users still cost one scrypt."""
    global _dummy_hash
    if stored is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password("not-a-real-password")
        stored, password = _dummy_hash, "definitely-wrong"
    return await asyncio.get_running_loop().run_in_executor(None, verify_password, password, stored)


# ------------------------------------------------------------------ auth

async def login(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    svc = request.app[SERVICES]
    ip = client_ip(request, svc.settings.trusted_proxy)
    if not svc.limiter.allowed(ip):
        return _error(429, "too many attempts", retry_after=svc.limiter.retry_after(ip))
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    if not isinstance(data, dict):
        return _error(400, "body must be an object")
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    user = await svc.store.user_get(username) if username else None
    ok = bool(password) and await _verify(password, user.password_hash if user else None)
    now = time.time()
    if not ok:
        svc.limiter.record_failure(ip)
        await svc.store.audit_append(now, f"ip:{ip}", "login.failed", username or None, {})
        return _error(401, "invalid username or password")
    svc.limiter.reset(ip)
    token = await svc.sessions.create(username, user_agent=request.headers.get("User-Agent"), ip=ip)
    await svc.store.audit_append(now, f"user:{username}", "login.ok", None, {"ip": ip})
    resp = web.Response(status=204)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", path="/",
                    max_age=int(svc.sessions.ttl_s), secure=is_https(request, svc.settings.trusted_proxy))
    return resp


async def logout(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    svc = request.app[SERVICES]
    await svc.sessions.revoke(request.cookies.get(SESSION_COOKIE, ""))
    await svc.store.audit_append(time.time(), f"user:{user.username}", "logout", None, {})
    resp = web.Response(status=204)
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def me(request: web.Request) -> web.Response:
    user = await require_user(request)
    row = await request.app[SERVICES].store.session_get(token_hash(request.cookies.get(SESSION_COOKIE, "")))
    return web.json_response({"username": user.username, "expires_at": row.expires_at if row else None})


# -------------------------------------------------------------- settings

async def settings_schema(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response({"groups": schema()})


async def settings_get(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response({"values": request.app[SERVICES].config.view_for_user()})


async def settings_put(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    if not isinstance(data, dict) or not data:
        return _error(400, "body must be a non-empty object of key: value")
    updates = {k: v for k, v in data.items() if v is not None}
    unsets = [k for k, v in data.items() if v is None]
    invalid = {}
    for key in unsets:
        try:
            spec_for(key)
        except KeyError:
            invalid[key] = "unknown setting"
    if invalid:
        return _error(400, "invalid settings", invalid=invalid)
    actor = f"user:{user.username}"
    config = request.app[SERVICES].config
    try:
        if updates:
            await config.set_many(updates, actor=actor)
    except SettingValidationError as e:
        return _error(400, "invalid settings", invalid=e.errors)
    for key in unsets:
        await config.unset(key, actor=actor)
    return web.Response(status=204)


# ---------------------------------------------------------------- tokens

def _token_dict(row) -> dict:
    return {"id": row.id, "name": row.name, "created_at": row.created_at,
            "last_used": row.last_used, "revoked_at": row.revoked_at}


async def tokens_list(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response([_token_dict(r) for r in await request.app[SERVICES].node_tokens.list()])


async def tokens_create(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    name = str((data or {}).get("name") or "").strip() if isinstance(data, dict) else ""
    if not name or len(name) > MAX_TOKEN_NAME:
        return _error(400, f"name must be 1-{MAX_TOKEN_NAME} characters")
    svc = request.app[SERVICES]
    id_, token = await svc.node_tokens.create(name)
    await svc.store.audit_append(time.time(), f"user:{user.username}", "token.create", id_, {"name": name})
    return web.json_response({"id": id_, "name": name, "token": token}, status=201)


async def tokens_revoke(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    svc = request.app[SERVICES]
    id_ = request.match_info["id"]
    if not await svc.node_tokens.revoke(id_):
        return _error(404, "no such active token")
    await svc.store.audit_append(time.time(), f"user:{user.username}", "token.revoke", id_, {})
    return web.Response(status=204)


# ----------------------------------------------------------------- audit

async def audit(request: web.Request) -> web.Response:
    await require_user(request)
    try:
        limit = int(request.query.get("limit", "50"))
        before = int(request.query["before"]) if "before" in request.query else None
    except ValueError:
        return _error(400, "limit and before must be integers")
    if not 1 <= limit <= 500:
        return _error(400, "limit must be between 1 and 500")
    rows = await request.app[SERVICES].store.audit_list(limit=limit, before_id=before)
    return web.json_response([{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
                               "target": r.target, "detail": r.detail} for r in rows])


# ---------------------------------------------------------------- config

async def config_pull(request: web.Request) -> web.Response:
    principal = await require_principal(request)
    svc = request.app[SERVICES]
    scope = request.query.get("scope", "")
    if scope not in svc.config.known_scopes():
        return _error(400, f"unknown scope {scope!r}")
    values = svc.config.view_for_scope(scope)
    actor = f"node:{principal.name}" if isinstance(principal, Node) else f"user:{principal.username}"
    await svc.store.audit_append(time.time(), actor, "config.pull", None,
                                 {"scope": scope, "keys": sorted(values)})
    return web.json_response({"scope": scope, "values": values, "generated_at": time.time()})


# ----------------------------------------------------------------- shell

async def shell(request: web.Request) -> web.StreamResponse:
    index = request.app[STATIC_DIR] / "index.html"
    if not index.is_file():
        return web.Response(status=404, text="dashboard files are not installed")
    if request.path != "/login" and not isinstance(await resolve_principal(request), User):
        raise web.HTTPFound("login")          # relative: works behind a path prefix
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


def add_web_routes(app: web.Application, static_dir: Path | None) -> None:
    static_dir = static_dir or DASHBOARD_DIR
    app[STATIC_DIR] = static_dir
    app.add_routes([
        web.post("/auth/login", login),
        web.post("/auth/logout", logout),
        web.get("/auth/me", me),
        web.get("/api/settings/schema", settings_schema),
        web.get("/api/settings", settings_get),
        web.put("/api/settings", settings_put),
        web.get("/api/tokens", tokens_list),
        web.post("/api/tokens", tokens_create),
        web.delete("/api/tokens/{id}", tokens_revoke),
        web.get("/api/audit", audit),
        web.get("/config", config_pull),
        web.get("/", shell),
        web.get("/login", shell),
        web.get("/settings", shell),
        web.get("/tokens", shell),
    ])
    if static_dir.is_dir():
        app.router.add_static("/static", static_dir, show_index=False)
```

In `friday/sentinel/api.py`, import `from friday.sentinel.web import add_web_routes` and, in `create_app`, after `app.add_routes([...])`, add:
```python
    add_web_routes(app, static_dir)
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_web.py tests/sentinel/test_api.py -q`
Expected: all PASS. If `test_shell_and_static` fails on the redirect `Location`, aiohttp may absolutise it — assert `resp.headers["Location"].endswith("login")` instead and keep the relative `HTTPFound("login")`.

---

### Task 9: CLI

**Files:**
- Create: `friday/sentinel/cli.py`
- Modify: `friday/sentinel/__main__.py`
- Test: `tests/sentinel/test_cli.py`

**Interfaces:**
- Produces: `cli.main(argv: list[str] | None = None) -> int` with subcommands `run` (default), `keygen`, `user set-password NAME [--password-stdin]`, `token create NAME`, `token list`, `token revoke ID`; `cli.open_store(settings) -> Store`.
- `friday.sentinel.__main__.main` delegates to `cli.main`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_cli.py`:
```python
import base64
import io

import pytest

from friday.core.storage import Store
from friday.sentinel import cli
from friday.sentinel.auth import token_hash, verify_password
from tests.conftest import TEST_MASTER_KEY


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    return tmp_path / "data"


def test_keygen_prints_env_line(capsys):
    assert cli.main(["keygen"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("FRIDAY_MASTER_KEY=")
    assert len(base64.urlsafe_b64decode(line.split("=", 1)[1])) == 32


def test_set_password_from_stdin(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    assert cli.main(["user", "set-password", "vince", "--password-stdin"]) == 0
    assert "vince" in capsys.readouterr().out
    store = Store.open(env / "sentinel.db")
    try:
        assert verify_password("hunter2", store.user_get("vince").password_hash)
        assert store.audit_list()[0].action == "user.set_password"
    finally:
        store.close()


def test_set_password_rejects_empty(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["user", "set-password", "vince", "--password-stdin"]) == 1
    assert "empty" in capsys.readouterr().err


def test_token_lifecycle(env, capsys):
    assert cli.main(["token", "create", "desktop"]) == 0
    out = capsys.readouterr().out
    token = next(word for word in out.split() if word.startswith("fn_"))
    store = Store.open(env / "sentinel.db")
    try:
        row = store.node_token_by_hash(token_hash(token))
        assert row.name == "desktop"
    finally:
        store.close()
    assert cli.main(["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "desktop" in listing and row.id in listing and token not in listing
    assert cli.main(["token", "revoke", row.id]) == 0
    assert cli.main(["token", "revoke", row.id]) == 1
    store = Store.open(env / "sentinel.db")
    try:
        assert store.node_token_by_hash(token_hash(token)) is None
    finally:
        store.close()


def test_store_commands_need_master_key(env, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_MASTER_KEY", "")        # empty beats any value in the developer's real .env
    assert cli.main(["token", "list"]) == 1
    assert "FRIDAY_MASTER_KEY" in capsys.readouterr().err


def test_main_module_delegates():
    from friday.sentinel.__main__ import main
    assert main is cli.main
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_cli.py -q`
Expected: `ImportError`.

- [ ] **Step 3: Implement**

`friday/sentinel/cli.py`:
```python
"""``friday-sentinel`` command line: run the daemon, or bootstrap it.

Bootstrap commands open the SQLite store directly. WAL and busy_timeout make
that safe while the daemon is running; every change is audited as
``actor="cli"``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sqlite3
import sys
import time

from friday.core.config import ConfigError, Settings, load_settings
from friday.core.storage import Store
from friday.core.vault import Vault
from friday.sentinel.auth import NODE_TOKEN_PREFIX, hash_password, new_token, token_hash


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="friday-sentinel", description="FRIDAY sentinel daemon and bootstrap tools")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start the daemon (default)")
    sub.add_parser("keygen", help="print a new FRIDAY_MASTER_KEY line for .env")
    user = sub.add_parser("user", help="dashboard user management").add_subparsers(dest="user_command")
    sp = user.add_parser("set-password", help="create the user or change its password")
    sp.add_argument("name")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    token = sub.add_parser("token", help="node token management").add_subparsers(dest="token_command")
    token.add_parser("create", help="mint a node token (printed once)").add_argument("name")
    token.add_parser("list", help="list node tokens")
    token.add_parser("revoke", help="revoke a node token").add_argument("id")
    return p


def open_store(settings: Settings) -> Store:
    Vault.from_master_key(settings.master_key or "")      # fail early with the keygen hint
    return Store.open(settings.data_dir / "sentinel.db", synchronous=settings.db_synchronous)


def _cmd_run() -> int:
    from friday.sentinel.daemon import Sentinel
    return asyncio.run(Sentinel(load_settings()).run())


def _cmd_keygen() -> int:
    print(f"FRIDAY_MASTER_KEY={Vault.generate_master_key()}")
    return 0


def _cmd_set_password(name: str, from_stdin: bool) -> int:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"Password for {name}: ")
        if password != getpass.getpass("Repeat password: "):
            print("friday-sentinel: passwords do not match", file=sys.stderr)
            return 1
    if not password:
        print("friday-sentinel: password must not be empty", file=sys.stderr)
        return 1
    store = open_store(load_settings())
    try:
        now = time.time()
        store.user_upsert(name, hash_password(password), now)
        store.audit_append(now, "cli", "user.set_password", name, {})
    finally:
        store.close()
    print(f"password set for user {name}")
    return 0


def _cmd_token_create(name: str) -> int:
    store = open_store(load_settings())
    try:
        token = new_token(NODE_TOKEN_PREFIX)
        id_ = secrets.token_hex(8)
        now = time.time()
        store.node_token_create(id_, name, token_hash(token), now)
        store.audit_append(now, "cli", "token.create", id_, {"name": name})
    finally:
        store.close()
    print(f"node token {id_} ({name}) created. Shown once — store it as FRIDAY_SENTINEL_TOKEN on the node:")
    print(token)
    return 0


def _cmd_token_list() -> int:
    store = open_store(load_settings())
    try:
        rows = store.node_tokens_list()
    finally:
        store.close()
    if not rows:
        print("no node tokens")
        return 0
    print(f"{'id':18} {'name':20} {'created':20} {'last used':20} status")
    for r in rows:
        status = "revoked" if r.revoked_at else "active"
        print(f"{r.id:18} {r.name:20} {_when(r.created_at):20} {_when(r.last_used):20} {status}")
    return 0


def _cmd_token_revoke(id_: str) -> int:
    store = open_store(load_settings())
    try:
        ok = store.node_token_revoke(id_, time.time())
        if ok:
            store.audit_append(time.time(), "cli", "token.revoke", id_, {})
    finally:
        store.close()
    if not ok:
        print(f"friday-sentinel: no active token with id {id_}", file=sys.stderr)
        return 1
    print(f"token {id_} revoked")
    return 0


def _when(ts: float | None) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in (None, "run"):
            return _cmd_run()
        if args.command == "keygen":
            return _cmd_keygen()
        if args.command == "user" and args.user_command == "set-password":
            return _cmd_set_password(args.name, args.password_stdin)
        if args.command == "token":
            if args.token_command == "create":
                return _cmd_token_create(args.name)
            if args.token_command == "list":
                return _cmd_token_list()
            if args.token_command == "revoke":
                return _cmd_token_revoke(args.id)
        _parser().print_help()
        return 2
    except ConfigError as e:
        print(f"friday-sentinel: configuration error: {e}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as e:
        print(f"friday-sentinel: cannot start: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
```

`friday/sentinel/__main__.py` becomes:
```python
"""``python -m friday.sentinel`` / ``friday-sentinel``."""

import sys

from friday.sentinel.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_cli.py -q`
Expected: all PASS.

---

### Task 10: Daemon wiring, housekeeping, daemon tests

**Files:**
- Modify: `friday/sentinel/daemon.py`, `friday/sentinel/monitors.py`, `tests/sentinel/test_daemon.py`

**Interfaces:**
- Produces on `Sentinel`: `services: Services | None` (set once the store is open and the vault verified).
- Daemon boot: `Vault.from_master_key` (→ `ConfigError` exit path), fingerprint logged, `RuntimeConfig.load()`, `Services` built, WARNING when `users_count() == 0`, `ApiServer(services)`.
- `Housekeeping.run` also calls `store.sessions_prune(now)`.

- [ ] **Step 1: Update and extend the daemon tests**

In `tests/sentinel/test_daemon.py`:
- add `from tests.conftest import TEST_MASTER_KEY` and put `"FRIDAY_MASTER_KEY": TEST_MASTER_KEY,` into `_settings`' env dict;
- change `test_main_reports_config_error` to call `main([])` and add `monkeypatch.setenv("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)` before it;
- append:
```python
async def test_boot_without_master_key_raises_config_error(tmp_path):
    settings = _settings(tmp_path, FRIDAY_MASTER_KEY="")
    with pytest.raises(ConfigError) as excinfo:
        await Sentinel(settings).run()
    assert "FRIDAY_MASTER_KEY" in str(excinfo.value)


async def test_services_are_wired_and_no_user_warning_logged(tmp_path, caplog):
    sentinel = Sentinel(_settings(tmp_path))
    with caplog.at_level(logging.WARNING, logger="friday.sentinel"):
        task = await _start(sentinel)
        assert sentinel.services is not None
        assert sentinel.services.config.get("controls.call_mode") == "urgent_only"
        assert sentinel.services.vault.fingerprint()
        sentinel.request_shutdown("done")
        assert await asyncio.wait_for(task, 10) == 0
    assert any("friday-sentinel user set-password" in r.getMessage() for r in caplog.records)


async def test_node_token_from_cli_store_works_against_running_daemon(tmp_path):
    settings = _settings(tmp_path)
    from friday.sentinel.auth import NODE_TOKEN_PREFIX, new_token, token_hash
    seed = Store.open(settings.data_dir / "sentinel.db")
    token = new_token(NODE_TOKEN_PREFIX)
    seed.node_token_create("cli1", "desktop", token_hash(token), time.time())
    seed.close()

    sentinel = Sentinel(settings)
    task = await _start(sentinel)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{sentinel.api_port}/nodes") as resp:
            assert resp.status == 401
        async with http.get(f"http://127.0.0.1:{sentinel.api_port}/nodes",
                            headers={"Authorization": f"Bearer {token}"}) as resp:
            assert resp.status == 200
    sentinel.request_shutdown("done")
    assert await asyncio.wait_for(task, 10) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_daemon.py -q`
Expected: `ApiServer(s, bus, store, self.state)` signature error and missing `services`.

- [ ] **Step 3: Implement**

In `friday/sentinel/daemon.py`:

Imports — replace `from friday.sentinel.api import ApiServer, HealthState` with:
```python
from friday.core.vault import Vault
from friday.sentinel.api import ApiServer
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.runtime_config import RuntimeConfig
from friday.sentinel.services import HealthState, Services
```

In `__init__` add `self.services: Services | None = None`.

In `run()`, replace the block from `bus = EventBus(store)` through `server = ApiServer(s, bus, store, self.state)` with:
```python
            vault = Vault.from_master_key(s.master_key or "")
            log.info("vault open (key fingerprint %s)", vault.fingerprint())

            bus = EventBus(store)
            ctx = HandlerContext(settings=s, store=store, bus=bus,
                                 logger=logging.getLogger("friday.sentinel.events"))
            for handler in (HeartbeatHandler(), TelemetryHandler(), LogHandler()):
                bus.subscribe(handler)

            config = RuntimeConfig(store, vault, s, bus)
            await config.load()
            services = Services(settings=s, store=store, bus=bus, state=self.state, vault=vault,
                                config=config, sessions=SessionManager(store),
                                node_tokens=NodeTokens(store), limiter=LoginLimiter())
            self.services = services
            if await store.users_count() == 0:
                log.warning("no dashboard user exists; create one with: "
                            "friday-sentinel user set-password <name>")

            for bridge in load_bridges(s):
                try:
                    await bridge.start(bus, ctx)
                    bridges.append(bridge)
                    log.info("bridge %s started", bridge.name)
                except Exception:
                    log.exception("bridge %s failed to start; skipping it",
                                  getattr(bridge, "name", bridge))

            server = ApiServer(services)
```

In `friday/sentinel/monitors.py`, `Housekeeping.run` becomes:
```python
    async def run(self, ctx: HandlerContext) -> None:
        while True:
            await asyncio.sleep(self.interval_s)          # nothing to prune at boot
            now = time.time()
            counts = await ctx.store.prune(now - ctx.settings.retention_days * 86400)
            sessions = await ctx.store.sessions_prune(now)
            await ctx.store.checkpoint("PASSIVE")
            ctx.logger.info("housekeeping: pruned %d telemetry rows, %d events, %d sessions",
                            counts["telemetry"], counts["events"], sessions)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 11: Dashboard static files

**Files:**
- Create: `friday/sentinel/dashboard/index.html`, `friday/sentinel/dashboard/style.css`, `friday/sentinel/dashboard/app.js`
- Test: `tests/sentinel/test_dashboard_files.py`

- [ ] **Step 1: Write the failing test**

`tests/sentinel/test_dashboard_files.py`:
```python
import re
import time

import pytest

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from friday.sentinel.web import DASHBOARD_DIR


def test_dashboard_files_exist_and_use_relative_urls():
    assert (DASHBOARD_DIR / "index.html").is_file()
    html = (DASHBOARD_DIR / "index.html").read_text()
    assert 'src="static/app.js"' in html and 'href="static/style.css"' in html
    js = (DASHBOARD_DIR / "app.js").read_text()
    assert "X-FRIDAY-Client" in js
    assert not re.search(r"""["']/(api|auth|static|config)""", js), "absolute URLs break reverse-proxy prefixes"


async def test_real_dashboard_is_served(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    client = await aiohttp_client(create_app(services))
    resp = await client.get("/login")
    assert resp.status == 200 and 'id="app"' in await resp.text()
    assert (await client.get("/static/style.css")).status == 200
    assert (await client.get("/static/app.js")).status == 200
    assert (await client.get("/", allow_redirects=False)).status == 302
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/sentinel/test_dashboard_files.py -q`
Expected: FAIL — files missing.

- [ ] **Step 3: Write the files**

`friday/sentinel/dashboard/index.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FRIDAY Sentinel</title>
  <link rel="stylesheet" href="static/style.css">
</head>
<body>
  <div id="app" class="app">
    <header class="topbar">
      <div class="brand"><span class="orb"></span> FRIDAY <span class="dim">sentinel</span></div>
      <nav id="nav" class="nav hidden">
        <a href="settings" data-view="settings">Settings</a>
        <a href="tokens" data-view="tokens">Nodes &amp; tokens</a>
        <button id="logout" class="ghost">Sign out</button>
      </nav>
    </header>
    <main id="main" class="main"></main>
    <div id="toast" class="toast hidden"></div>
  </div>
  <script src="static/app.js"></script>
</body>
</html>
```

`friday/sentinel/dashboard/style.css`:
```css
:root {
  --bg: #070a0f; --panel: rgba(255,255,255,0.04); --line: rgba(255,255,255,0.08);
  --text: #e6edf3; --dim: #8b98a5; --cyan: #00f2fe; --amber: #ffb800; --red: #e5726f; --green: #00ff88;
  --mono: "SF Mono", "JetBrains Mono", Menlo, monospace;
  --sans: -apple-system, "Inter", "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: radial-gradient(1200px 600px at 20% -10%, #0d1a2b 0%, var(--bg) 60%); color: var(--text); font-family: var(--sans); }
.app { max-width: 960px; margin: 0 auto; padding: 0 16px 48px; }
.topbar { display: flex; align-items: center; justify-content: space-between; padding: 18px 0; border-bottom: 1px solid var(--line); }
.brand { font-weight: 600; letter-spacing: 0.08em; display: flex; align-items: center; gap: 10px; }
.brand .dim { font-weight: 400; color: var(--dim); letter-spacing: 0.04em; }
.orb { width: 14px; height: 14px; border-radius: 50%; background: radial-gradient(circle at 35% 35%, #fff 0, var(--cyan) 35%, transparent 70%); box-shadow: 0 0 18px var(--cyan); }
.nav { display: flex; gap: 18px; align-items: center; }
.nav a { color: var(--dim); text-decoration: none; }
.nav a.active { color: var(--cyan); }
.hidden { display: none !important; }
.main { padding-top: 24px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 18px; backdrop-filter: blur(8px); }
.card h2 { margin: 0 0 14px; font-size: 13px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--dim); }
.login { max-width: 360px; margin: 60px auto; }
label { display: block; font-size: 12px; color: var(--dim); margin: 12px 0 6px; }
input, select { width: 100%; padding: 10px 12px; background: rgba(0,0,0,0.35); color: var(--text); border: 1px solid var(--line); border-radius: 8px; font: inherit; }
input:focus, select:focus { outline: none; border-color: var(--cyan); }
button { font: inherit; padding: 10px 16px; border-radius: 8px; border: 1px solid var(--cyan); background: rgba(0,242,254,0.12); color: var(--text); cursor: pointer; }
button.ghost { border-color: var(--line); background: transparent; color: var(--dim); }
button.danger { border-color: var(--red); background: rgba(229,114,111,0.12); }
button:disabled { opacity: 0.5; cursor: default; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; align-items: end; }
@media (max-width: 640px) { .row { grid-template-columns: 1fr; } }
.field .desc { font-size: 12px; color: var(--dim); margin-top: 4px; }
.field .err { font-size: 12px; color: var(--red); margin-top: 4px; }
.badge { display: inline-block; font-family: var(--mono); font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--dim); margin-left: 8px; }
.badge.vault { color: var(--green); border-color: rgba(0,255,136,0.4); }
.badge.env { color: var(--amber); border-color: rgba(255,184,0,0.4); }
.badge.undecryptable { color: var(--red); border-color: rgba(229,114,111,0.5); }
.actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); }
th { font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--dim); }
.mono { font-family: var(--mono); }
.token-reveal { font-family: var(--mono); word-break: break-all; padding: 12px; background: rgba(0,0,0,0.4); border-radius: 8px; border: 1px dashed var(--amber); }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); padding: 10px 16px; border-radius: 10px; background: #111a26; border: 1px solid var(--line); }
.toast.error { border-color: var(--red); }
```

`friday/sentinel/dashboard/app.js`:
```js
/* FRIDAY sentinel dashboard — vanilla JS, relative URLs only (works behind a path prefix). */
(function () {
  "use strict";
  const main = document.getElementById("main");
  const nav = document.getElementById("nav");
  const toastEl = document.getElementById("toast");
  const state = { user: null, schema: null };

  // ---------------------------------------------------------------- http
  async function api(path, options) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    opts.headers = Object.assign({ "X-FRIDAY-Client": "dashboard" }, opts.headers);
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers["Content-Type"] = "application/json";
      delete opts.json;
    }
    const resp = await fetch(path, opts);
    if (resp.status === 401 && path !== "auth/login") {
      state.user = null;
      render("login");
      throw new Error("signed out");
    }
    let body = null;
    const text = await resp.text();
    if (text) { try { body = JSON.parse(text); } catch (e) { body = text; } }
    if (!resp.ok) {
      const err = new Error((body && body.error) || `HTTP ${resp.status}`);
      err.status = resp.status; err.body = body;
      throw err;
    }
    return body;
  }

  function toast(message, isError) {
    toastEl.textContent = message;
    toastEl.className = "toast" + (isError ? " error" : "");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => toastEl.classList.add("hidden"), 3500);
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.append(c instanceof Node ? c : document.createTextNode(String(c))));
    return node;
  }

  // --------------------------------------------------------------- views
  function render(view) {
    main.replaceChildren();
    nav.classList.toggle("hidden", !state.user);
    nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
    ({ login: renderLogin, settings: renderSettings, tokens: renderTokens })[view]();
  }

  function renderLogin() {
    const user = el("input", { id: "u", autocomplete: "username" });
    const pass = el("input", { id: "p", type: "password", autocomplete: "current-password" });
    const err = el("div", { class: "err" });
    const form = el("form", {
      class: "card login",
      onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        try {
          await api("auth/login", { method: "POST", json: { username: user.value, password: pass.value } });
          await boot();
        } catch (ex) {
          err.textContent = ex.status === 429 ? `Too many attempts — retry in ${ex.body.retry_after}s` : ex.message;
        }
      },
    }, [el("h2", {}, ["Sign in"]), el("label", {}, ["Username"]), user, el("label", {}, ["Password"]), pass,
        err, el("div", { class: "actions" }, [el("button", { type: "submit" }, ["Sign in"])])]);
    main.append(form);
    user.focus();
  }

  async function renderSettings() {
    const [schema, current] = await Promise.all([api("api/settings/schema"), api("api/settings")]);
    const values = Object.fromEntries(current.values.map((v) => [v.key, v]));
    const inputs = {};
    const errors = {};
    schema.groups.forEach((group) => {
      const card = el("div", { class: "card" }, [el("h2", {}, [group.name])]);
      const grid = el("div", { class: "row" });
      group.keys.forEach((spec) => {
        const cur = values[spec.key] || {};
        let input;
        if (spec.type === "enum") {
          input = el("select", {}, spec.choices.map((c) => el("option", { value: c, selected: c === cur.value ? "" : null }, [c])));
        } else if (spec.type === "bool") {
          input = el("select", {}, [["true", "on"], ["false", "off"]].map(([v, label]) =>
            el("option", { value: v, selected: String(cur.value) === v ? "" : null }, [label])));
        } else if (spec.secret) {
          input = el("input", { type: "password", placeholder: cur.set ? `set (…${cur.hint})` : "not set", autocomplete: "new-password" });
        } else {
          input = el("input", { value: cur.value === null || cur.value === undefined ? "" : cur.value });
        }
        input.dataset.original = spec.secret ? "" : (input.value ?? "");
        inputs[spec.key] = { input, spec };
        errors[spec.key] = el("div", { class: "err" });
        grid.append(el("div", { class: "field" }, [
          el("label", {}, [spec.key, el("span", { class: `badge ${cur.source || "default"}` }, [cur.source || "default"])]),
          input, el("div", { class: "desc" }, [spec.description]), errors[spec.key]]));
      });
      card.append(grid);
      main.append(card);
    });
    const save = el("button", {
      onclick: async () => {
        Object.values(errors).forEach((e) => (e.textContent = ""));
        const changes = {};
        Object.entries(inputs).forEach(([key, { input, spec }]) => {
          const v = input.value;
          if (spec.secret ? v !== "" : v !== input.dataset.original) changes[key] = spec.type === "bool" ? v === "true" : v;
        });
        if (!Object.keys(changes).length) return toast("Nothing changed");
        try {
          await api("api/settings", { method: "PUT", json: changes });
          toast("Saved");
          render("settings");
        } catch (ex) {
          Object.entries((ex.body && ex.body.invalid) || {}).forEach(([k, m]) => { if (errors[k]) errors[k].textContent = m; });
          toast(ex.message, true);
        }
      },
    }, ["Save changes"]);
    main.append(el("div", { class: "actions" }, [save]));
  }

  async function renderTokens() {
    const [tokens, nodes] = await Promise.all([api("api/tokens"), api("nodes")]);
    const name = el("input", { placeholder: "e.g. desktop, pixel" });
    const reveal = el("div", { class: "hidden" });
    const create = el("button", {
      onclick: async () => {
        try {
          const made = await api("api/tokens", { method: "POST", json: { name: name.value } });
          reveal.className = "";
          reveal.replaceChildren(
            el("p", {}, [`Token for ${made.name} — shown once. Put it in that node's .env as FRIDAY_SENTINEL_TOKEN.`]),
            el("div", { class: "token-reveal" }, [made.token]),
            el("div", { class: "actions" }, [el("button", { class: "ghost", onclick: () => navigator.clipboard.writeText(made.token).then(() => toast("Copied")) }, ["Copy"])]));
          name.value = "";
          renderTokenTable();
        } catch (ex) { toast(ex.message, true); }
      },
    }, ["Create token"]);
    main.append(el("div", { class: "card" }, [el("h2", {}, ["New node token"]), el("label", {}, ["Name"]), name,
      el("div", { class: "actions" }, [create]), reveal]));
    const tableCard = el("div", { class: "card" }, [el("h2", {}, ["Node tokens"])]);
    main.append(tableCard);
    function renderTokenTable() {
      api("api/tokens").then((rows) => {
        const table = el("table", {}, [el("tr", {}, ["ID", "Name", "Created", "Last used", "Status", ""].map((h) => el("th", {}, [h])))]);
        rows.forEach((t) => table.append(el("tr", {}, [
          el("td", { class: "mono" }, [t.id]), el("td", {}, [t.name]), el("td", {}, [when(t.created_at)]),
          el("td", {}, [when(t.last_used)]), el("td", {}, [t.revoked_at ? "revoked" : "active"]),
          el("td", {}, [t.revoked_at ? "" : el("button", { class: "danger", onclick: async () => {
            try { await api(`api/tokens/${t.id}`, { method: "DELETE" }); toast("Revoked"); renderTokenTable(); }
            catch (ex) { toast(ex.message, true); }
          } }, ["Revoke"])])])));
        tableCard.replaceChildren(el("h2", {}, ["Node tokens"]), rows.length ? table : el("p", { class: "dim" }, ["No tokens yet."]));
      });
    }
    renderTokenTable();
    const nodeTable = el("table", {}, [el("tr", {}, ["Node", "Status", "Last seen", "Platform"].map((h) => el("th", {}, [h])))]);
    nodes.forEach((n) => nodeTable.append(el("tr", {}, [el("td", { class: "mono" }, [n.node_id]), el("td", {}, [n.status]),
      el("td", {}, [when(n.last_seen)]), el("td", {}, [(n.meta && n.meta.platform) || ""])])));
    main.append(el("div", { class: "card" }, [el("h2", {}, ["Nodes (last heartbeat)"]), nodes.length ? nodeTable : el("p", {}, ["No heartbeats yet."])]));
  }

  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }

  // ---------------------------------------------------------------- boot
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-view]");
    if (a) { e.preventDefault(); history.pushState({}, "", a.getAttribute("href")); render(a.dataset.view); }
  });
  document.getElementById("logout").addEventListener("click", async () => {
    try { await api("auth/logout", { method: "POST" }); } catch (e) { /* already signed out */ }
    state.user = null; history.pushState({}, "", "login"); render("login");
  });
  window.addEventListener("popstate", () => route());

  function route() {
    const leaf = location.pathname.split("/").filter(Boolean).pop() || "";
    render(!state.user ? "login" : (leaf === "tokens" ? "tokens" : "settings"));
  }

  async function boot() {
    try { state.user = await api("auth/me"); } catch (e) { state.user = null; }
    if (state.user && (location.pathname.endsWith("/login") || location.pathname.endsWith("/"))) history.replaceState({}, "", "settings");
    route();
  }
  boot();
})();
```

- [ ] **Step 4: Run to verify it passes, then look at it**

Run: `.venv/bin/pytest tests/sentinel/test_dashboard_files.py tests/sentinel/test_web.py -q`
Expected: all PASS.

Manual: `FRIDAY_MASTER_KEY=$(.venv/bin/python -m friday.sentinel keygen | cut -d= -f2) .venv/bin/python -m friday.sentinel user set-password vince --password-stdin <<< 'pw'` is **wrong** (each invocation needs the same key) — instead put a key in `.env` first (Task 13 documents it), then `user set-password vince`, run the daemon, open `http://127.0.0.1:8770/`, sign in, save a Gemini key, create a token, revoke it. Confirm the source badge flips `env → vault` after saving the Gemini key.

---

### Task 12: Desktop pulls configuration from the sentinel

**Files:**
- Create: `friday/desktop/config_pull.py`
- Modify: `friday/desktop/sentinel_client.py`, `friday/desktop/hub.py`, `friday/desktop/agents.py`, `friday/desktop/widget_generator.py`, `tests/desktop/test_sentinel_client.py`, `tests/desktop/test_imports.py`
- Test: `tests/desktop/test_config_pull.py`

**Interfaces:**
- Produces: `SentinelClient.fetch_config(scope="desktop") -> dict | None`; `config_pull.pull_or_cached(settings, fetch, cache_path) -> tuple[Settings, str]` (source ∈ `"sentinel" | "cache" | "env"`); `agents.TIERS[tier]["role"]`; `agents.model_for(tier) -> str`; `widget_generator.widget_model() -> str`; `hub.MODEL_ID`/`hub.LIVE_VOICE` re-resolved inside `run_friday()`; `hub.CONFIG_CACHE_FILE`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/desktop/test_sentinel_client.py`:
```python
async def test_fetch_config_success(aiohttp_server):
    async def config(request):
        assert request.headers["Authorization"] == "Bearer tok" and request.query["scope"] == "desktop"
        return web.json_response({"scope": "desktop", "values": {"llm.gemini_api_key": "k"}, "generated_at": 1.0})
    app = web.Application()
    app.add_routes([web.get("/sentinel/config", config)])
    server = await aiohttp_server(app)
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "tok", "mac")
    try:
        assert await client.fetch_config() == {"llm.gemini_api_key": "k"}
    finally:
        await client.aclose()


async def test_fetch_config_failures_return_none(aiohttp_server):
    app = web.Application()
    app.add_routes([web.get("/sentinel/config", lambda r: web.json_response({"error": "x"}, status=401))])
    server = await aiohttp_server(app)
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "tok", "mac")
    try:
        assert await client.fetch_config() is None
    finally:
        await client.aclose()
    dead = SentinelClient("http://127.0.0.1:9/sentinel", "tok", "mac", timeout_s=0.5)
    try:
        assert await dead.fetch_config() is None
    finally:
        await dead.aclose()
```

`tests/desktop/test_config_pull.py`:
```python
import json
import os
import stat

from friday.desktop.config_pull import pull_or_cached


async def test_sentinel_success_overlays_and_caches(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key")
    cache = tmp_path / "cache.json"

    async def fetch():
        return {"llm.gemini_api_key": "vault-key", "llm.routes.live": "gemini:live-v", "desktop.voice": "Kore"}

    out, source = await pull_or_cached(settings, fetch, cache)
    assert source == "sentinel"
    assert out.gemini_api_key == "vault-key" and out.llm_routes["live"] == "gemini:live-v" and out.friday_voice == "Kore"
    assert settings.gemini_api_key == "env-key"
    assert json.loads(cache.read_text())["values"]["llm.gemini_api_key"] == "vault-key"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600


async def test_failure_falls_back_to_cache_then_env(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key")
    cache = tmp_path / "cache.json"

    async def fail():
        return None

    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "env" and out.gemini_api_key == "env-key"

    cache.write_text(json.dumps({"values": {"llm.gemini_api_key": "cached-key"}, "fetched_at": 1.0}))
    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "cache" and out.gemini_api_key == "cached-key"

    cache.write_text("{not json")
    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "env" and out.gemini_api_key == "env-key"
```

In `tests/desktop/test_imports.py` replace `test_hub_models_come_from_routing` with:
```python
def test_hub_models_come_from_routing():
    from friday.core.llm import resolve
    from friday.desktop import agents, hub, widget_generator

    assert hub.MODEL_ID == resolve(hub.settings, "live").model
    assert agents.TIERS["os"]["role"] == "agent_os" and agents.TIERS["spatial"]["role"] == "agent_spatial"
    assert agents.model_for("os") == resolve(hub.settings, "agent_os").model
    assert widget_generator.widget_model() == resolve(hub.settings, "widget").model
    assert str(hub.CONFIG_CACHE_FILE).startswith(str(hub.settings.data_dir))
```
and add `"friday.desktop.config_pull"` to `MODULES`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/desktop -q`
Expected: failures on `fetch_config`, `config_pull`, `model_for`, `widget_model`, `CONFIG_CACHE_FILE`.

- [ ] **Step 3: Implement**

Add to `friday/desktop/sentinel_client.py` (inside `SentinelClient`, after `post`):
```python
    async def fetch_config(self, scope: str = "desktop") -> dict | None:
        """Pull this node's configuration. None on any failure (never raises)."""
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = self._events_url[: -len("/events")] + "/config"
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            async with self._session.get(url, params={"scope": scope}, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                body = await resp.json()
        except Exception as e:
            (log.debug if self._failing else log.warning)("sentinel config pull from %s failed: %s", url, e)
            self._failing = True
            return None
        values = body.get("values") if isinstance(body, dict) else None
        return values if isinstance(values, dict) else None
```

`friday/desktop/config_pull.py`:
```python
"""Overlay the sentinel's configuration onto local Settings, with a cache.

Order of preference: a live pull, then the last good pull cached on disk,
then the local .env as loaded. The cache is written with mode 0600 — it
holds the same secrets the plaintext .env it supersedes did.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from friday.core.config import Settings, apply_overrides

log = logging.getLogger(__name__)


def _write_cache(path: Path, values: dict[str, Any]) -> None:
    payload = json.dumps({"values": values, "fetched_at": time.time()}, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    os.chmod(path, 0o600)


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    values = data.get("values") if isinstance(data, dict) else None
    return values if isinstance(values, dict) else None


async def pull_or_cached(settings: Settings, fetch: Callable[[], Awaitable[dict | None]],
                         cache_path: Path) -> tuple[Settings, str]:
    values = await fetch()
    if values is not None:
        try:
            _write_cache(cache_path, values)
        except OSError as e:
            log.warning("could not write config cache %s: %s", cache_path, e)
        return apply_overrides(settings, values), "sentinel"
    cached = _read_cache(cache_path)
    if cached is not None:
        return apply_overrides(settings, cached), "cache"
    return settings, "env"
```

In `friday/desktop/hub.py`:

Imports — add `from friday.core.config import get_settings, override_settings` (replace the existing `get_settings` import line) and `from friday.desktop import config_pull`.

After `MEMORY_FILE = ...` add:
```python
CONFIG_CACHE_FILE = os.path.join(DATA_DIR, "config-cache.json")
```

At the top of `run_friday()` (right after `main_loop = asyncio.get_running_loop()` and the signal-handler loop), add:
```python
    global settings, MODEL_ID, LIVE_VOICE
    if settings.sentinel_url:
        puller = SentinelClient(settings.sentinel_url, settings.sentinel_token, settings.node_id)
        try:
            settings, source = await config_pull.pull_or_cached(
                settings, puller.fetch_config, Path(CONFIG_CACHE_FILE))
        finally:
            await puller.aclose()
        override_settings(settings)
        log_info(f"Configuration source: {source}")
    MODEL_ID = resolve(settings, "live").model
    LIVE_VOICE = settings.friday_voice
```
(`from pathlib import Path` goes with the other imports.) The module-level `MODEL_ID` / `LIVE_VOICE` assignments stay as import-time defaults.

`friday/desktop/agents.py`:
- delete the `OS_AGENT_MODEL` / `SVE_AGENT_MODEL` lines;
- in `TIERS`, replace `"model": OS_AGENT_MODEL,` with `"role": "agent_os",` and `"model": SVE_AGENT_MODEL,` with `"role": "agent_spatial",`;
- add after `resolve_tier`:
```python
def model_for(tier: str) -> str:
    """The routed model for a tier, resolved per call so a config pull applies."""
    return resolve(get_settings(), TIERS[resolve_tier(tier)]["role"]).model
```
- in `run_agent`, change `model=spec["model"]` to `model=model_for(tier)`.

`friday/desktop/widget_generator.py`:
- replace `WIDGET_MODEL = resolve(get_settings(), "widget").model      # FRIDAY_LLM_WIDGET` with:
```python
def widget_model() -> str:
    return resolve(get_settings(), "widget").model          # FRIDAY_LLM_WIDGET; per call so a config pull applies
```
- change `model=WIDGET_MODEL` to `model=widget_model()`.

Run: `grep -nE 'OS_AGENT_MODEL|SVE_AGENT_MODEL|WIDGET_MODEL|spec\["model"\]' friday/desktop/*.py` → expected: no output.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/desktop -q && .venv/bin/pytest -q`
Expected: all PASS.

Manual end-to-end (needs a node token and the real Gemini key in the vault):
```bash
# sentinel side
.venv/bin/python -m friday.sentinel token create desktop        # copy the fn_… token
FRIDAY_SENTINEL_BIND=127.0.0.1:8770 .venv/bin/python -m friday.sentinel &
# desktop side
FRIDAY_SENTINEL_URL=http://127.0.0.1:8770 FRIDAY_SENTINEL_TOKEN=fn_… .venv/bin/python -m friday.desktop.hub
```
Expected: `[FRIDAY Engine] Configuration source: sentinel`, then `Listening`; `data/config-cache.json` exists with mode `-rw-------`; the sentinel's audit shows `config.pull` from `node:desktop`. Stop the sentinel and rerun the hub: `Configuration source: cache`.

---

### Task 13: Docs and final verification

**Files:**
- Modify: `.env.template`, `deploy/README.md`, `readme.md`

- [ ] **Step 1: `.env.template`**

Replace the `shared` and `sentinel` sections so the file reads, top to bottom:
```ini
# ============================================================ host bootstrap
# Only what a node needs before it can reach the vault. Everything else —
# API keys, model routes, integration credentials, call mode — is managed in
# the sentinel dashboard and stored encrypted in data/sentinel.db.

# Where this node keeps its state. Default: <repo>/data
#FRIDAY_DATA_DIR=/var/lib/friday
# How this node names itself on the event bus. Default: hostname.
#FRIDAY_NODE_ID=vince-mac
FRIDAY_LOG_LEVEL=INFO

# ------------------------------------------------------------- sentinel only
# REQUIRED on the sentinel. Generate with:  .venv/bin/python -m friday.sentinel keygen
# Losing this key makes every stored secret unreadable; back it up separately.
FRIDAY_MASTER_KEY=
FRIDAY_SENTINEL_BIND=127.0.0.1:8770
# Set to true only when a reverse proxy (Caddy/Nginx/Tailscale Serve) in front
# of the sentinel sets X-Forwarded-Proto / X-Forwarded-For.
FRIDAY_TRUSTED_PROXY=false
FRIDAY_TELEMETRY_INTERVAL=15
FRIDAY_HEARTBEAT_INTERVAL=30
FRIDAY_RETENTION_DAYS=14
FRIDAY_DB_SYNCHRONOUS=FULL
#FRIDAY_BRIDGES=

# -------------------------------------------------------------- desktop only
# Where the Mac finds the sentinel and the node token it was issued
# (dashboard → Nodes & tokens, or: friday-sentinel token create desktop).
#FRIDAY_SENTINEL_URL=https://server.tailnet.ts.net/sentinel
#FRIDAY_SENTINEL_TOKEN=fn_...

# ============================================================ legacy values
# Still honoured when the vault has no value for the key (the dashboard shows
# them with an "env" badge). Prefer entering them in the dashboard.
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-live-preview
FRIDAY_VOICE=Aoede
#TRIPO_API_KEY=
#FRIDAY_LLM_AGENT_OS=gemini:gemini-3.8-flash
#FRIDAY_LLM_AGENT_SPATIAL=gemini:gemini-3.8-flash
#FRIDAY_LLM_WIDGET=gemini:gemini-3.7-flash
#FRIDAY_LLM_TRIAGE=gemini:gemini-3.7-flash
TESTING_MODE=true
```

- [ ] **Step 2: `deploy/README.md`**

Replace the "Any node: install" section with:
````markdown
## Sentinel: first boot

```bash
git clone <repo> && cd friday-ai-assistant
python3 -m venv .venv
.venv/bin/pip install -e ".[sentinel]"
cp .env.template .env
.venv/bin/python -m friday.sentinel keygen >> .env        # sets FRIDAY_MASTER_KEY — back it up
.venv/bin/python -m friday.sentinel user set-password vince
.venv/bin/python -m friday.sentinel token create desktop   # paste into the Mac's .env as FRIDAY_SENTINEL_TOKEN
.venv/bin/python -m friday.sentinel                        # foreground smoke run
```

Open `http://127.0.0.1:8770/` (or the Tailscale URL), sign in, and enter the
Gemini key under **Settings → llm**. The desktop pulls it on its next boot.

Behind Caddy/Nginx/Tailscale Serve set `FRIDAY_TRUSTED_PROXY=true` so the
session cookie is marked `Secure` from the forwarded scheme.
````
and in the API table add the rows:
```
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | session | dashboard sign-in (cookie: HttpOnly, SameSite=Lax, Secure over HTTPS) |
| `GET/PUT /api/settings`, `GET /api/settings/schema` | session | vault-backed settings; secrets masked |
| `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` | session | node tokens (plaintext shown once) |
| `GET /api/audit` | session | who changed what |
| `GET /config?scope=desktop` | node or session | decrypted config for a node scope; audited |
```
and change the auth column of `/events`, `/ws`, `/nodes`, `/telemetry` to `node token or session`.

- [ ] **Step 3: `readme.md`**

In **🛰️ Multi-Node Architecture** add a subsection after "What the sentinel does":
```markdown
### Configuration vault and dashboard

The sentinel is the configuration authority. `.env` holds host bootstrap only
(`FRIDAY_MASTER_KEY`, bind address, data dir); everything else — API keys, per-role
model routes, later the Jira/Google/telephony credentials — lives in `data/sentinel.db`,
AES-GCM-encrypted under a key derived from `FRIDAY_MASTER_KEY`, with the setting key
bound as associated data so a ciphertext cannot be moved between rows. A single
dashboard user (created with `friday-sentinel user set-password`) signs in at `/`;
sessions are HttpOnly/SameSite=Lax cookies stored hashed; every login, settings change,
token issue and config pull is audited. Nodes authenticate with vault-managed tokens
(`friday-sentinel token create <name>`, shown once). The desktop pulls its LLM
configuration from `GET /config?scope=desktop` at boot, caches the last good copy at
`data/config-cache.json` (mode 0600) and falls back to its `.env` when the sentinel is
unreachable. Legacy `.env` values are still honoured when the vault has none and show
with an `env` badge in the dashboard.
```
In **Running FRIDAY**, after the sentinel block, add the bootstrap lines (`keygen`, `user set-password`, `token create`) and "open `http://127.0.0.1:8770/`". In **📁 Repository Structure** add `vault.py` under `core/`, and `settings_registry.py`, `runtime_config.py`, `auth.py`, `services.py`, `principals.py`, `web.py`, `cli.py`, `dashboard/` under `sentinel/`, and `config_pull.py` under `desktop/`; add `config-cache.json` to the runtime-files table. In **Tech Stack** add `cryptography` (AES-GCM vault).

- [ ] **Step 4: Verify docs against source**

Run: `.venv/bin/python -c "import friday.sentinel.cli as c; c.main(['--help'])" | head -5` and `grep -n 'FRIDAY_SENTINEL_TOKEN' readme.md deploy/README.md .env.template | head` — every documented command and variable must exist in the code.

- [ ] **Step 5: Final verification**

```bash
.venv/bin/pytest -q                                  # all green, 0 skipped on the Mac
.venv/bin/pytest tests/test_boundaries.py -q         # cryptography allowed, nothing else leaked
git status --short | wc -l
```
Then the manual flow from Task 11 and Task 12 once each. Leave everything uncommitted.
