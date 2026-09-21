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
                detail = str(e) or (f"exceeded {self._handler_timeout_s:.0f}s"
                                    if isinstance(e, asyncio.TimeoutError) else "")
                errors.append(f"{handler.name}: {type(e).__name__}: {detail}")
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
