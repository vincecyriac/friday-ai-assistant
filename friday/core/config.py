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
