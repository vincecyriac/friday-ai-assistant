"""HTTP + WebSocket surface of the sentinel.

Bound to localhost by default and fronted by Tailscale Serve for remote
nodes, exactly like the desktop hub. ``POST /events`` is how any node or
bridge pushes into the bus; ``/ws`` is the push channel out. When
``FRIDAY_SENTINEL_TOKEN`` is set both require it.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

import friday
from friday.core.config import Settings
from friday.core.events import Event, EventValidationError
from friday.core.platform import PlatformInfo
from friday.core.storage import AsyncStore
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext, matches

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
MAX_BATCH = 100


@dataclass
class HealthState:
    started_at: float
    platform: PlatformInfo
    restarts: dict[str, int] = field(default_factory=dict)


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


SETTINGS = web.AppKey("settings", Settings)
BUS = web.AppKey("bus", EventBus)
STORE = web.AppKey("store", AsyncStore)
STATE = web.AppKey("state", HealthState)
FANOUT = web.AppKey("fanout", WebSocketFanout)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _authorized(request: web.Request, token: str | None, *, allow_query: bool = False) -> bool:
    if token is None:
        return True
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    if not supplied and allow_query:
        supplied = request.query.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


async def health(request: web.Request) -> web.Response:
    app = request.app
    state = app[STATE]
    return web.json_response({
        "status": "ok",
        "node_id": app[SETTINGS].node_id,
        "version": friday.__version__,
        "uptime_s": max(0.0, time.time() - state.started_at),
        "platform": state.platform.to_dict(),
        "queue": await app[STORE].queue_depths(),
        "supervisor_restarts": dict(state.restarts),
    })


async def telemetry(request: web.Request) -> web.Response:
    snapshot = await request.app[STORE].telemetry_latest(request.app[SETTINGS].node_id)
    return web.Response(status=204) if snapshot is None else web.json_response(snapshot)


async def nodes(request: web.Request) -> web.Response:
    rows = await request.app[STORE].heartbeats()
    return web.json_response([{"node_id": r.node_id, "last_seen": r.last_seen,
                               "status": r.status, "meta": r.meta} for r in rows])


async def post_events(request: web.Request) -> web.Response:
    app = request.app
    if not _authorized(request, app[SETTINGS].sentinel_token):
        return _error(401, "missing or invalid bearer token")
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

    ids = [await app[BUS].publish(event) for event in events]
    return web.json_response({"ids": ids}, status=202)


async def websocket(request: web.Request) -> web.StreamResponse:
    app = request.app
    if not _authorized(request, app[SETTINGS].sentinel_token, allow_query=True):
        return _error(401, "missing or invalid token")
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    fanout = app[FANOUT]
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


def create_app(settings: Settings, bus: EventBus, store: AsyncStore,
               state: HealthState) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    fanout = WebSocketFanout()
    bus.subscribe(fanout)
    app[SETTINGS] = settings
    app[BUS] = bus
    app[STORE] = store
    app[STATE] = state
    app[FANOUT] = fanout
    app.add_routes([
        web.get("/health", health),
        web.get("/telemetry", telemetry),
        web.get("/nodes", nodes),
        web.post("/events", post_events),
        web.get("/ws", websocket),
    ])
    return app


class ApiServer:
    def __init__(self, settings: Settings, bus: EventBus, store: AsyncStore, state: HealthState):
        self._settings = settings
        self._bus = bus
        self.app = create_app(settings, bus, store, state)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def fanout(self) -> WebSocketFanout:
        return self.app[FANOUT]

    async def start(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._settings.sentinel_bind_host, self._settings.sentinel_bind_port)
        await site.start()
        self._runner = runner
        addresses = runner.addresses
        self.port = addresses[0][1] if addresses else self._settings.sentinel_bind_port

    async def stop(self) -> None:
        self._bus.unsubscribe(self.fanout)
        await self.fanout.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
