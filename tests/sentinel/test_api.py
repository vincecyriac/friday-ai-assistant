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
