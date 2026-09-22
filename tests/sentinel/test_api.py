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
