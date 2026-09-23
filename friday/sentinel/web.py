"""Dashboard-facing routes: login, settings, node tokens, audit, config pull,
the SPA shell and its static files.

CSRF posture: the session cookie is SameSite=Lax and every state-changing
request must carry ``X-FRIDAY-Client: dashboard`` — a cross-origin page
cannot add a custom header without a CORS preflight, and no CORS headers are
served. Origin is checked against Host as a second layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from friday.sentinel.assistant import DEFAULT_TITLE, run_turn
from friday.sentinel.auth import (SESSION_COOKIE, Node, User, client_ip, hash_password, is_https,
                                  token_hash, verify_password)
from friday.sentinel.principals import require_principal, require_user, resolve_principal
from friday.sentinel.services import SERVICES
from friday.sentinel.settings_registry import SettingValidationError, schema, spec_for
from friday.sentinel.voice import VoiceSession
from friday.webassets import WEBASSETS_DIR

log = logging.getLogger(__name__)

CLIENT_HEADER = "X-FRIDAY-Client"
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
STATIC_DIR = web.AppKey("static_dir", Path)
MAX_TOKEN_NAME = 64
_dummy_hash: str | None = None


def _error(status: int, message: str, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _expected_host(request: web.Request) -> str:
    settings = request.app[SERVICES].settings
    if settings.trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.host


def _csrf_check(request: web.Request) -> web.Response | None:
    if request.headers.get(CLIENT_HEADER) != "dashboard":
        return _error(403, "missing client header")
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != _expected_host(request):
        return _error(403, "origin mismatch")
    return None


async def _verify(password: str, stored: str | None) -> bool:
    """Constant-work verification: unknown users still cost one scrypt."""
    global _dummy_hash
    if stored is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password("not-a-real-password")
        stored, password = _dummy_hash, "definitely-wrong"
    return await asyncio.get_running_loop().run_in_executor(None, verify_password, password, stored)


# ------------------------------------------------------------------ auth

async def login(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    svc = request.app[SERVICES]
    ip = client_ip(request, svc.settings.trusted_proxy)
    if not svc.limiter.allowed(ip):
        return _error(429, "too many attempts", retry_after=svc.limiter.retry_after(ip))
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    if not isinstance(data, dict):
        return _error(400, "body must be an object")
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    user = await svc.store.user_get(username) if username else None
    ok = bool(password) and await _verify(password, user.password_hash if user else None)
    now = time.time()
    if not ok:
        svc.limiter.record_failure(ip)
        await svc.audit(f"ip:{ip}", "login.failed", username or None, {})
        return _error(401, "invalid username or password")
    svc.limiter.reset(ip)
    token = await svc.sessions.create(username, user_agent=request.headers.get("User-Agent"), ip=ip)
    await svc.audit(f"user:{username}", "login.ok", None, {"ip": ip})
    resp = web.Response(status=204)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", path="/",
                    max_age=int(svc.sessions.ttl_s), secure=is_https(request, svc.settings.trusted_proxy))
    return resp


async def logout(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    svc = request.app[SERVICES]
    await svc.sessions.revoke(request.cookies.get(SESSION_COOKIE, ""))
    await svc.audit(f"user:{user.username}", "logout", None, {})
    resp = web.Response(status=204)
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def me(request: web.Request) -> web.Response:
    user = await require_user(request)
    row = await request.app[SERVICES].store.session_get(token_hash(request.cookies.get(SESSION_COOKIE, "")))
    return web.json_response({"username": user.username, "expires_at": row.expires_at if row else None})


# -------------------------------------------------------------- settings

async def settings_schema(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response({"groups": schema()})


async def settings_get(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response({"values": request.app[SERVICES].config.view_for_user()})


async def settings_put(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    if not isinstance(data, dict) or not data:
        return _error(400, "body must be a non-empty object of key: value")
    updates = {k: v for k, v in data.items() if v is not None}
    unsets = [k for k, v in data.items() if v is None]
    invalid = {}
    for key in unsets:
        try:
            spec_for(key)
        except KeyError:
            invalid[key] = "unknown setting"
    if invalid:
        return _error(400, "invalid settings", invalid=invalid)
    actor = f"user:{user.username}"
    config = request.app[SERVICES].config
    try:
        if updates:
            await config.set_many(updates, actor=actor)
    except SettingValidationError as e:
        return _error(400, "invalid settings", invalid=e.errors)
    for key in unsets:
        await config.unset(key, actor=actor)
    return web.Response(status=204)


# ---------------------------------------------------------------- tokens

def _token_dict(row) -> dict:
    return {"id": row.id, "name": row.name, "created_at": row.created_at,
            "last_used": row.last_used, "revoked_at": row.revoked_at}


async def tokens_list(request: web.Request) -> web.Response:
    await require_user(request)
    return web.json_response([_token_dict(r) for r in await request.app[SERVICES].node_tokens.list()])


async def tokens_create(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    name = str((data or {}).get("name") or "").strip() if isinstance(data, dict) else ""
    if not name or len(name) > MAX_TOKEN_NAME:
        return _error(400, f"name must be 1-{MAX_TOKEN_NAME} characters")
    svc = request.app[SERVICES]
    id_, token = await svc.node_tokens.create(name)
    await svc.audit(f"user:{user.username}", "token.create", id_, {"name": name})
    return web.json_response({"id": id_, "name": name, "token": token}, status=201)


async def tokens_revoke(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    svc = request.app[SERVICES]
    id_ = request.match_info["id"]
    if not await svc.node_tokens.revoke(id_):
        return _error(404, "no such active token")
    await svc.audit(f"user:{user.username}", "token.revoke", id_, {})
    return web.Response(status=204)


# ----------------------------------------------------------------- audit

async def audit(request: web.Request) -> web.Response:
    await require_user(request)
    try:
        limit = int(request.query.get("limit", "50"))
        before = int(request.query["before"]) if "before" in request.query else None
    except ValueError:
        return _error(400, "limit and before must be integers")
    if not 1 <= limit <= 500:
        return _error(400, "limit must be between 1 and 500")
    rows = await request.app[SERVICES].store.audit_list(limit=limit, before_id=before)
    return web.json_response([{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
                               "target": r.target, "detail": r.detail} for r in rows])


# ----------------------------------------------------------------- events

def _float_query(request: web.Request, name: str) -> float | None:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{name} must be a number"}),
                                 content_type="application/json") from None


async def events_list(request: web.Request) -> web.Response:
    await require_user(request)
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        return _error(400, "limit must be an integer")
    if not 1 <= limit <= 500:
        return _error(400, "limit must be between 1 and 500")
    since, before = _float_query(request, "since"), _float_query(request, "before")
    rows = await request.app[SERVICES].store.list_events(
        type_glob=request.query.get("type") or "*", source=request.query.get("source") or None,
        since_ts=since, before_ts=before, limit=limit)
    return web.json_response([{
        "id": r.event.id, "ts": r.event.ts, "type": r.event.type, "source": r.event.source,
        "payload": r.event.payload, "priority": r.event.priority, "status": r.status} for r in rows])


async def telemetry_all(request: web.Request) -> web.Response:
    await require_principal(request)
    return web.json_response({"nodes": await request.app[SERVICES].store.telemetry_latest_all()})


# ------------------------------------------------------------------ chat

MAX_CHAT_CONTENT = 8000


def _conversation_dict(row) -> dict:
    return {"id": row.id, "title": row.title, "created_at": row.created_at,
            "updated_at": row.updated_at, "message_count": row.message_count}


def _message_dict(row) -> dict:
    return {"id": row.id, "seq": row.seq, "role": row.role, "content": row.content,
            "tool_name": row.tool_name, "tool_args": row.tool_args, "tool_result": row.tool_result,
            "status": row.status, "ts": row.ts, "via": row.via}


async def chat_list(request: web.Request) -> web.Response:
    await require_user(request)
    rows = await request.app[SERVICES].store.conversations_list()
    return web.json_response([_conversation_dict(r) for r in rows])


async def chat_create(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    await require_user(request)
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    title = str((data or {}).get("title") or "").strip() if isinstance(data, dict) else ""
    row = await request.app[SERVICES].store.conversation_create(
        secrets.token_hex(8), title[:60] or DEFAULT_TITLE, time.time())
    return web.json_response(_conversation_dict(row), status=201)


async def chat_get(request: web.Request) -> web.Response:
    await require_user(request)
    store = request.app[SERVICES].store
    conv = await store.conversation_get(request.match_info["id"])
    if conv is None:
        return _error(404, "no such conversation")
    messages = await store.messages_list(conv.id)
    return web.json_response({"conversation": _conversation_dict(conv),
                              "messages": [_message_dict(m) for m in messages]})


async def chat_delete(request: web.Request) -> web.Response:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    await require_user(request)
    svc = request.app[SERVICES]
    if not await svc.store.conversation_delete(request.match_info["id"]):
        return _error(404, "no such conversation")
    svc.chat_locks.pop(request.match_info["id"], None)
    return web.Response(status=204)


async def chat_message(request: web.Request) -> web.StreamResponse:
    denied = _csrf_check(request)
    if denied is not None:
        return denied
    user = await require_user(request)
    svc = request.app[SERVICES]
    conv_id = request.match_info["id"]
    if await svc.store.conversation_get(conv_id) is None:
        return _error(404, "no such conversation")
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "body is not valid JSON")
    content = str((data or {}).get("content") or "").strip() if isinstance(data, dict) else ""
    if not 1 <= len(content) <= MAX_CHAT_CONTENT:
        return _error(400, f"content must be 1-{MAX_CHAT_CONTENT} characters")
    lock = svc.chat_locks.setdefault(conv_id, asyncio.Lock())
    if lock.locked():
        return _error(409, "a turn is in progress")
    async with lock:
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "application/x-ndjson", "Cache-Control": "no-store",
            "X-Accel-Buffering": "no"})
        await resp.prepare(request)

        async def emit(obj: dict) -> None:
            await resp.write((json.dumps(obj) + "\n").encode("utf-8"))

        await run_turn(svc, conversation_id=conv_id, user=user, text=content, emit=emit)
        try:
            await resp.write_eof()
        except ConnectionResetError:
            pass
        return resp


# ----------------------------------------------------------------- voice

async def voice_ws(request: web.Request) -> web.StreamResponse:
    principal = await require_principal(request)
    svc = request.app[SERVICES]
    if not svc.config.get("voice.enabled"):
        return _error(503, "voice is disabled")

    conv_id = request.query.get("conversation") or ""
    if not conv_id or await svc.store.conversation_get(conv_id) is None:
        row = await svc.store.conversation_create(secrets.token_hex(8), DEFAULT_TITLE, time.time())
        conv_id = row.id
    if conv_id in svc.voice_sessions:
        return _error(409, "a voice session is already open for this conversation")

    user = principal if isinstance(principal, User) else User(f"node:{principal.name}")
    ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    await ws.send_json({"type": "state", "value": "connecting", "conversation_id": conv_id})

    session = VoiceSession(svc, user, conv_id, ws)
    svc.voice_sessions[conv_id] = session
    runner = asyncio.create_task(session.run())
    try:
        async for msg in ws:
            if msg.type is WSMsgType.BINARY:
                session.feed_audio(msg.data)
            elif msg.type is WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "text" and data.get("content"):
                    await session.feed_text(str(data["content"]))
    except Exception as e:
        log.info("voice socket ended: %s", e)
    finally:
        session.stop()
        svc.voice_sessions.pop(conv_id, None)
        try:
            await asyncio.wait_for(runner, timeout=5)
        except (asyncio.TimeoutError, Exception):
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    return ws


# ---------------------------------------------------------------- config

async def config_pull(request: web.Request) -> web.Response:
    principal = await require_principal(request)
    svc = request.app[SERVICES]
    scope = request.query.get("scope", "")
    if scope not in svc.config.known_scopes():
        return _error(400, f"unknown scope {scope!r}")
    values = svc.config.view_for_scope(scope)
    actor = f"node:{principal.name}" if isinstance(principal, Node) else f"user:{principal.username}"
    await svc.audit(actor, "config.pull", None,
                                 {"scope": scope, "keys": sorted(values)})
    return web.json_response({"scope": scope, "values": values, "generated_at": time.time()})


# ----------------------------------------------------------------- shell

async def shell(request: web.Request) -> web.StreamResponse:
    index = request.app[STATIC_DIR] / "index.html"
    if not index.is_file():
        return web.Response(status=404, text="dashboard files are not installed")
    if request.path != "/login" and not isinstance(await resolve_principal(request), User):
        raise web.HTTPFound("login")          # relative: works behind a path prefix
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


def add_web_routes(app: web.Application, static_dir: Path | None) -> None:
    static_dir = static_dir or DASHBOARD_DIR
    app[STATIC_DIR] = static_dir
    app.add_routes([
        web.post("/auth/login", login),
        web.post("/auth/logout", logout),
        web.get("/auth/me", me),
        web.get("/api/settings/schema", settings_schema),
        web.get("/api/settings", settings_get),
        web.put("/api/settings", settings_put),
        web.get("/api/tokens", tokens_list),
        web.post("/api/tokens", tokens_create),
        web.delete("/api/tokens/{id}", tokens_revoke),
        web.get("/api/audit", audit),
        web.get("/api/chat", chat_list),
        web.post("/api/chat", chat_create),
        web.get("/api/chat/{id}", chat_get),
        web.delete("/api/chat/{id}", chat_delete),
        web.post("/api/chat/{id}/messages", chat_message),
        web.get("/api/events", events_list),
        web.get("/api/telemetry", telemetry_all),
        web.get("/config", config_pull),
        web.get("/voice/ws", voice_ws),
        web.get("/", shell),
        web.get("/login", shell),
        web.get("/overview", shell),
        web.get("/activity", shell),
        web.get("/controls", shell),
        web.get("/assistant", shell),
        web.get("/settings", shell),
        web.get("/tokens", shell),
    ])
    if static_dir.is_dir():
        app.router.add_static("/static", static_dir, show_index=False)
    if WEBASSETS_DIR.is_dir():
        app.router.add_static("/shared", WEBASSETS_DIR, show_index=False)
