"""One audit write = one row + one ``audit.entry`` event, so the dashboard's
activity stream shows who changed what as it happens. Detail is secret-free by
construction (RuntimeConfig never puts a secret value in it)."""

from __future__ import annotations

import time
from typing import Any, Mapping

from friday.core.events import Event
from friday.core.storage import AsyncStore


async def record(store: AsyncStore, bus, node_id: str, actor: str, action: str,
                 target: str | None, detail: Mapping[str, Any]) -> int:
    ts = time.time()
    detail = dict(detail)
    row_id = await store.audit_append(ts, actor, action, target, detail)
    if bus is not None:
        await bus.publish(Event(type="audit.entry", source=node_id, payload={
            "id": row_id, "ts": ts, "actor": actor, "action": action,
            "target": target, "detail": detail}))
    return row_id
