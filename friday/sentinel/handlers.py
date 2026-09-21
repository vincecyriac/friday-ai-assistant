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
