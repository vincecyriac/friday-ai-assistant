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
