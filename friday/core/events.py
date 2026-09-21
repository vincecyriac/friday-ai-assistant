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
