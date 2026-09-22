"""HTTP + WebSocket surface of the sentinel.

Bound to localhost by default and fronted by Tailscale Serve for remote
nodes, exactly like the desktop hub. ``POST /events`` is how any node or
bridge pushes into the bus; ``/ws`` is the push channel out. Every route
except ``/health`` requires a principal: a dashboard session cookie or a
vault-managed node token (see ``principals.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web

import friday
from friday.core.events import Event, EventValidationError
from friday.sentinel.handlers import HandlerContext, matches
from friday.sentinel.principals import require_principal
from friday.sentinel.services import SERVICES, HealthState, Services   # noqa: F401  (HealthState re-exported)
from friday.sentinel.web import add_web_routes

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
MAX_BATCH = 100


@dataclass
class _Subscription:
    ws: web.WebSocketResponse
    patterns: tuple[str, ...]


class WebSocketFanout:
    """Bus handler that pushes every matching event to connected sockets.

    Never raises: a socket that cannot be written within ``send_timeout_s``
    is closed and forgotten, and the event is unaffected.
    """

    name = "ws_fanout"
    patterns = ("*",)

    def __init__(self, send_timeout_s: float = 2.0):
        self._subs: list[_Subscription] = []
        self._send_timeout_s = send_timeout_s

    def add(self, ws: web.WebSocketResponse, patterns=("*",)) -> _Subscription:
        sub = _Subscription(ws, tuple(patterns))
        self._subs.append(sub)
        return sub

    def remove(self, sub: _Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    @property
    def connections(self) -> int:
        return len(self._subs)

    async def handle(self, event: Event, ctx: HandlerContext) -> None:
        if not self._subs:
            return
        text = json.dumps(event.to_dict())
        for sub in list(self._subs):
            if sub.ws.closed:
                self.remove(sub)
                continue
            if not matches(sub.patterns, event.type):
                continue
            try:
                await asyncio.wait_for(sub.ws.send_str(text), self._send_timeout_s)
            except Exception as e:
                log.info("dropping slow or dead websocket client: %s", e)
                self.remove(sub)
                await self._close(sub.ws, 1011)

    async def close_all(self, code: int = 1001) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            await self._close(sub.ws, code)

    @staticmethod
    async def _close(ws: web.WebSocketResponse, code: int) -> None:
        try:
            await ws.close(code=code, message=b"shutting down")
        except Exception:
            pass


FANOUT = web.AppKey("fanout", WebSocketFanout)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def health(request: web.Request) -> web.Response:
    svc = request.app[SERVICES]
    return web.json_response({
        "status": "ok",
        "node_id": svc.settings.node_id,
        "version": friday.__version__,
        "uptime_s": max(0.0, time.time() - svc.state.started_at),
        "platform": svc.state.platform.to_dict(),
        "queue": await svc.store.queue_depths(),
        "supervisor_restarts": dict(svc.state.restarts),
    })


async def telemetry(request: web.Request) -> web.Response:
    await require_principal(request)
    svc = request.app[SERVICES]
    snapshot = await svc.store.telemetry_latest(svc.settings.node_id)
    return web.Response(status=204) if snapshot is None else web.json_response(snapshot)


async def nodes(request: web.Request) -> web.Response:
    await require_principal(request)
    rows = await request.app[SERVICES].store.heartbeats()
    return web.json_response([{"node_id": r.node_id, "last_seen": r.last_seen,
                               "status": r.status, "meta": r.meta} for r in rows])


async def post_events(request: web.Request) -> web.Response:
    await require_principal(request)
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _error(413, f"body exceeds {MAX_BODY_BYTES} bytes")
    if len(body) > MAX_BODY_BYTES:
        return _error(413, f"body exceeds {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(body)
    except ValueError:
        return _error(400, "body is not valid JSON")
    items = data if isinstance(data, list) else [data]
    if not items:
        return _error(400, "no events in body")
    if len(items) > MAX_BATCH:
        return _error(400, f"at most {MAX_BATCH} events per request")

    now = time.time()
    events: list[Event] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return _error(400, f"event {index} is not an object")
        item = dict(item)
        item.setdefault("ts", now)
        try:
            events.append(Event.from_dict(item))
        except EventValidationError as e:
            return _error(400, f"event {index}: {e}")

    bus = request.app[SERVICES].bus
    ids = [await bus.publish(event) for event in events]
    return web.json_response({"ids": ids}, status=202)


async def websocket(request: web.Request) -> web.StreamResponse:
    await require_principal(request, allow_query=True)
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    fanout = request.app[FANOUT]
    sub = fanout.add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                await ws.send_json({"error": "message is not JSON"})
                continue
            patterns = data.get("subscribe") if isinstance(data, dict) else None
            if (isinstance(patterns, list) and patterns
                    and all(isinstance(p, str) and p for p in patterns)):
                sub.patterns = tuple(patterns)
                await ws.send_json({"subscribed": list(sub.patterns)})
            else:
                await ws.send_json({"error": 'expected {"subscribe": ["pattern", ...]}'})
    finally:
        fanout.remove(sub)
    return ws


def create_app(services: Services, *, static_dir: Path | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    fanout = WebSocketFanout()
    services.bus.subscribe(fanout)
    app[SERVICES] = services
    app[FANOUT] = fanout
    app.add_routes([
        web.get("/health", health),
        web.get("/telemetry", telemetry),
        web.get("/nodes", nodes),
        web.post("/events", post_events),
        web.get("/ws", websocket),
    ])
    add_web_routes(app, static_dir)
    return app


class ApiServer:
    def __init__(self, services: Services, *, static_dir: Path | None = None):
        self._services = services
        self.app = create_app(services, static_dir=static_dir)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def fanout(self) -> WebSocketFanout:
        return self.app[FANOUT]

    async def start(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._services.settings.sentinel_bind_host,
                           self._services.settings.sentinel_bind_port)
        await site.start()
        self._runner = runner
        addresses = runner.addresses
        self.port = addresses[0][1] if addresses else self._services.settings.sentinel_bind_port

    async def stop(self) -> None:
        self._services.bus.unsubscribe(self.fanout)
        await self.fanout.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
