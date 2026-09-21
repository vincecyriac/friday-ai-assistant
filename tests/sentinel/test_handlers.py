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
