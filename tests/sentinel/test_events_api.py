import time

import pytest

from friday.core.events import Event
from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from tests.sentinel.conftest import node_headers

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
async def client(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    c = await aiohttp_client(create_app(services))
    assert (await c.post("/auth/login", json={"username": "vince", "password": "pw"}, headers=CSRF)).status == 204
    return c


async def test_events_list_filters_and_shape(client, services):
    # Types no built-in handler matches, so the running dispatcher does not mutate payloads.
    for i, (t, src, ts) in enumerate([("node.custom", "mac", 1.0), ("telemetry.custom", "mac", 2.0),
                                      ("audit.entry", "s", 3.0)]):
        await services.store.enqueue(Event(type=t, source=src, id=f"e{i}", ts=ts, payload={"i": i}))
    rows = await (await client.get("/api/events?before=100")).json()      # excludes the login's audit event
    assert [r["id"] for r in rows] == ["e2", "e1", "e0"]
    status = rows[0].pop("status")
    assert status in ("pending", "processing", "done")                    # the dispatcher may have run
    assert rows[0] == {"id": "e2", "ts": 3.0, "type": "audit.entry", "source": "s",
                       "payload": {"i": 2}, "priority": 0}
    assert [r["id"] for r in await (await client.get("/api/events?type=node.*")).json()] == ["e0"]
    assert [r["id"] for r in await (await client.get("/api/events?source=mac&since=2")).json()] == ["e1"]
    assert [r["id"] for r in await (await client.get("/api/events?before=3&limit=1")).json()] == ["e1"]
    for bad in ("limit=0", "limit=501", "since=x", "before=y", "limit=abc"):
        assert (await client.get(f"/api/events?{bad}")).status == 400


async def test_events_and_telemetry_auth(aiohttp_client, services, node_token):
    anon = await aiohttp_client(create_app(services))
    assert (await anon.get("/api/events")).status == 401
    assert (await anon.get("/api/telemetry")).status == 401
    assert (await anon.get("/api/events", headers=node_headers(node_token))).status == 401   # session only
    assert (await anon.get("/api/telemetry", headers=node_headers(node_token))).status == 200


async def test_telemetry_all_nodes(client, services):
    assert await (await client.get("/api/telemetry")).json() == {"nodes": {}}
    await services.store.telemetry_insert("mac", {"cpu_percent": 1.0}, ts=1.0)
    await services.store.telemetry_insert("pi", {"cpu_percent": 2.0}, ts=1.0)
    body = await (await client.get("/api/telemetry")).json()
    assert set(body["nodes"]) == {"mac", "pi"} and body["nodes"]["pi"]["cpu_percent"] == 2.0


async def test_shell_routes(client, aiohttp_client, services):
    for path in ("/overview", "/activity", "/controls", "/assistant"):
        assert (await client.get(path, allow_redirects=False)).status == 200
    anon = await aiohttp_client(create_app(services))
    resp = await anon.get("/assistant", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "login"
