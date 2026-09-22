import logging

from aiohttp import web

from friday.core.events import Event
from friday.desktop.sentinel_client import SentinelClient


def _app(status=202, received=None):
    async def events(request):
        if received is not None:
            received.append((dict(request.headers), await request.json()))
        return web.json_response({"ids": ["x"]}, status=status)

    app = web.Application()
    app.add_routes([web.post("/sentinel/events", events)])
    return app


async def test_post_success_sends_bearer_and_body(aiohttp_server):
    received = []
    server = await aiohttp_server(_app(received=received))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel/", "tok", "mac")
    try:
        ok = await client.post(Event(type="node.heartbeat", source="mac", payload={"status": "x"}))
    finally:
        await client.aclose()
    assert ok is True
    headers, body = received[0]
    assert headers["Authorization"] == "Bearer tok"
    assert body["type"] == "node.heartbeat" and body["source"] == "mac"


async def test_post_without_token_sends_no_header(aiohttp_server):
    received = []
    server = await aiohttp_server(_app(received=received))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", None, "mac")
    try:
        assert await client.post(Event(type="a.b", source="mac")) is True
    finally:
        await client.aclose()
    assert "Authorization" not in received[0][0]


async def test_non_202_is_false(aiohttp_server):
    server = await aiohttp_server(_app(status=500))
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "t", "mac")
    try:
        assert await client.post(Event(type="a.b", source="mac")) is False
    finally:
        await client.aclose()


async def test_unreachable_is_false_and_warns_once(caplog):
    client = SentinelClient("http://127.0.0.1:9/sentinel", "t", "mac", timeout_s=0.5)
    try:
        with caplog.at_level(logging.DEBUG, logger="friday.desktop.sentinel_client"):
            assert await client.post(Event(type="a.b", source="mac")) is False
            assert await client.post(Event(type="a.b", source="mac")) is False
    finally:
        await client.aclose()
    levels = [r.levelno for r in caplog.records if "sentinel" in r.getMessage()]
    assert levels[0] == logging.WARNING
    assert all(level == logging.DEBUG for level in levels[1:])


async def test_fetch_config_success(aiohttp_server):
    async def config(request):
        assert request.headers["Authorization"] == "Bearer tok" and request.query["scope"] == "desktop"
        return web.json_response({"scope": "desktop", "values": {"llm.gemini_api_key": "k"}, "generated_at": 1.0})
    app = web.Application()
    app.add_routes([web.get("/sentinel/config", config)])
    server = await aiohttp_server(app)
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "tok", "mac")
    try:
        assert await client.fetch_config() == {"llm.gemini_api_key": "k"}
    finally:
        await client.aclose()


async def test_fetch_config_failures_return_none(aiohttp_server):
    async def denied(request):
        return web.json_response({"error": "x"}, status=401)
    app = web.Application()
    app.add_routes([web.get("/sentinel/config", denied)])
    server = await aiohttp_server(app)
    client = SentinelClient(f"http://127.0.0.1:{server.port}/sentinel", "tok", "mac")
    try:
        assert await client.fetch_config() is None
    finally:
        await client.aclose()
    dead = SentinelClient("http://127.0.0.1:9/sentinel", "tok", "mac", timeout_s=0.5)
    try:
        assert await dead.fetch_config() is None
    finally:
        await dead.aclose()
