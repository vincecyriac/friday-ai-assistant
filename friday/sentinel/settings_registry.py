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
from friday.sentinel.sources.jira import DEFAULT_JQL

GROUP_ORDER = ("llm", "desktop", "sentinel", "sources", "voice", "controls")


class SettingValidationError(ValueError):
    """A value does not fit its SettingSpec. ``errors`` maps key → message."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.errors = errors or {}


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


def _range(lo: float, hi: float) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if not lo <= value <= hi:
            raise ValueError(f"must be between {lo:g} and {hi:g}")
        return value
    return check


REGISTRY: tuple[SettingSpec, ...] = (
    SettingSpec("llm.gemini_api_key", "str", "llm", "Google Gemini API key",
                secret=True, scopes=("desktop",), env="GEMINI_API_KEY"),
    _route_spec("live", "gemini:gemini-3.1-flash-live-preview", scopes=("desktop",)),
    _route_spec("agent_os", "gemini:gemini-3.8-flash", scopes=("desktop",)),
    _route_spec("agent_spatial", "gemini:gemini-3.8-flash", scopes=("desktop",)),
    _route_spec("widget", "gemini:gemini-3.7-flash", scopes=("desktop",)),
    _route_spec("triage", "gemini:gemini-3.7-flash", scopes=()),
    _route_spec("assistant", "gemini:gemini-3.7-flash", scopes=()),
    SettingSpec("llm.tripo_api_key", "str", "llm", "Tripo3D API key (text-to-3D); optional",
                secret=True, scopes=("desktop",), env="TRIPO_API_KEY"),
    SettingSpec("desktop.voice", "str", "desktop", "Gemini Live voice for the desktop (Aoede or Kore)",
                default="Aoede", scopes=("desktop",), env="FRIDAY_VOICE"),
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
    SettingSpec("voice.enabled", "bool", "voice",
                "Allow the dashboard to open a voice session with FRIDAY", default=True),
    SettingSpec("voice.name", "enum", "voice",
                "The sentinel's Gemini Live voice (the desktop has its own)",
                default="Aoede", choices=("Aoede", "Kore", "Charon", "Fenrir", "Puck")),
    SettingSpec("controls.call_mode", "enum", "controls",
                "When escalations may place a phone call",
                default="urgent_only", choices=("always", "urgent_only", "mute")),
    SettingSpec("controls.dnd", "bool", "controls", "Do not disturb: suppress calls and pings",
                default=False),
    SettingSpec("sentinel.telemetry_interval_s", "float", "sentinel",
                "Seconds between telemetry samples on the sentinel host",
                default=15.0, env="FRIDAY_TELEMETRY_INTERVAL", validator=_range(1, 3600)),
    SettingSpec("sentinel.heartbeat_interval_s", "float", "sentinel",
                "Seconds between the sentinel's own heartbeats (also the watchdog ping)",
                default=30.0, env="FRIDAY_HEARTBEAT_INTERVAL", validator=_range(1, 3600)),
    SettingSpec("sentinel.retention_days", "int", "sentinel",
                "Days of telemetry and finished events to keep",
                default=14, env="FRIDAY_RETENTION_DAYS", validator=_range(1, 365)),
    SettingSpec("sentinel.chat_retention_days", "int", "sentinel",
                "Days an idle assistant conversation is kept",
                default=90, validator=_range(1, 3650)),
    SettingSpec("controls.monitors.email", "bool", "controls",
                "Watch the mailbox (read + drafts); arrives with the monitors release", default=False),
    SettingSpec("controls.monitors.calendar", "bool", "controls",
                "Watch the calendar and guard meetings; arrives with the monitors release", default=False),
    SettingSpec("controls.monitors.jira", "bool", "controls",
                "Watch Jira for blockers (read-only); arrives with the monitors release", default=False),
)

_BY_KEY = {spec.key: spec for spec in REGISTRY}


def spec_for(key: str) -> SettingSpec:
    return _BY_KEY[key]


def _fail(spec: SettingSpec, message: str) -> SettingValidationError:
    return SettingValidationError(f"{spec.key}: {message}", {spec.key: message})


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
