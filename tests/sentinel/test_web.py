import asyncio
import time

import aiohttp
import pytest

from friday.core.events import Event
from friday.sentinel.api import create_app
from friday.sentinel.auth import SESSION_COOKIE, hash_password
from tests.sentinel.conftest import build_services, node_headers, teardown_services

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "dash"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>FRIDAY</title><script src=\"static/app.js\"></script>")
    (d / "app.js").write_text("console.log('hi')")
    return d


@pytest.fixture
async def client(aiohttp_client, services, static_dir):
    await services.store.user_upsert("vince", hash_password("pw-correct"), ts=time.time())
    return await aiohttp_client(create_app(services, static_dir=static_dir))


async def login(client, password="pw-correct"):
    return await client.post("/auth/login", json={"username": "vince", "password": password}, headers=CSRF)


async def status_as_node(client, path, token) -> int:
    """GET without the test client's cookie jar, so only the bearer token counts."""
    async with aiohttp.ClientSession() as http:
        async with http.get(client.make_url(path), headers=node_headers(token)) as resp:
            return resp.status


# ------------------------------------------------------------------ auth

async def test_login_requires_csrf_header(client):
    resp = await client.post("/auth/login", json={"username": "vince", "password": "pw-correct"})
    assert resp.status == 403 and "client header" in (await resp.json())["error"]


async def test_login_wrong_password_is_401_and_audited(client, services):
    assert (await login(client, "nope")).status == 401
    assert (await login(client, "")).status == 401
    resp = await client.post("/auth/login", json={"username": "ghost", "password": "x"}, headers=CSRF)
    assert resp.status == 401
    rows = await services.store.audit_list()
    assert [r.action for r in rows][:3] == ["login.failed"] * 3
    assert rows[0].target == "ghost" and rows[0].actor.startswith("ip:")


async def test_login_ok_sets_hardened_cookie(client, services):
    resp = await login(client)
    assert resp.status == 204
    cookie = resp.cookies[SESSION_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie["path"] == "/"
    assert int(cookie["max-age"]) == int(services.sessions.ttl_s)
    assert not cookie["secure"]
    me = await client.get("/auth/me")
    assert me.status == 200 and (await me.json())["username"] == "vince"
    assert (await me.json())["expires_at"] > time.time()
    assert (await services.store.audit_list())[0].action == "login.ok"


async def test_secure_flag_follows_forwarded_proto_only_when_trusted(aiohttp_client, make_settings, tmp_path, static_dir):
    for trusted, expected in (("false", False), ("true", True)):
        services = await build_services(make_settings, tmp_path / trusted, FRIDAY_TRUSTED_PROXY=trusted)
        await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
        client = await aiohttp_client(create_app(services, static_dir=static_dir))
        try:
            resp = await client.post("/auth/login", json={"username": "vince", "password": "pw"},
                                     headers={**CSRF, "X-Forwarded-Proto": "https"})
            assert resp.status == 204
            assert bool(resp.cookies[SESSION_COOKIE]["secure"]) is expected
        finally:
            await client.close()
            await teardown_services(services)


async def test_lockout_after_repeated_failures(client, services):
    for _ in range(3):
        assert (await login(client, "bad")).status == 401
    resp = await login(client)
    assert resp.status == 429
    body = await resp.json()
    assert body["error"] == "too many attempts" and 0 < body["retry_after"] <= 60


async def test_me_and_api_require_session(client):
    assert (await client.get("/auth/me")).status == 401
    assert (await client.get("/api/settings")).status == 401
    assert (await client.get("/api/settings/schema")).status == 401
    assert (await client.get("/api/tokens")).status == 401
    assert (await client.get("/api/audit")).status == 401


async def test_logout_clears_session(client, services):
    await login(client)
    resp = await client.post("/auth/logout", headers=CSRF)
    assert resp.status == 204
    assert (await client.get("/auth/me")).status == 401
    assert (await services.store.audit_list())[0].action == "logout"


async def test_state_changing_api_requires_csrf_header(client):
    await login(client)
    assert (await client.put("/api/settings", json={"controls.dnd": True})).status == 403
    assert (await client.post("/api/tokens", json={"name": "x"})).status == 403
    assert (await client.post("/auth/logout")).status == 403


async def test_origin_must_match_host(client):
    await login(client)
    resp = await client.put("/api/settings", json={"controls.dnd": True},
                            headers={**CSRF, "Origin": "https://evil.example"})
    assert resp.status == 403
    resp = await client.put("/api/settings", json={"controls.dnd": True},
                            headers={**CSRF, "Origin": f"http://{client.host}:{client.port}"})
    assert resp.status == 204


# -------------------------------------------------------------- settings

async def test_settings_schema_get_put(client, services):
    await login(client)
    schema = await (await client.get("/api/settings/schema")).json()
    assert [g["name"] for g in schema["groups"]] == ["llm", "desktop", "controls"]

    values = {v["key"]: v for v in (await (await client.get("/api/settings")).json())["values"]}
    assert values["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": False,
                                            "hint": "", "source": "default"}
    assert values["controls.call_mode"]["value"] == "urgent_only"

    resp = await client.put("/api/settings", json={"llm.gemini_api_key": "sk-1234567890abcdef",
                                                   "controls.call_mode": "mute"}, headers=CSRF)
    assert resp.status == 204
    values = {v["key"]: v for v in (await (await client.get("/api/settings")).json())["values"]}
    assert values["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": True,
                                            "hint": "cdef", "source": "vault"}
    assert values["controls.call_mode"]["value"] == "mute"
    assert services.config.get("llm.gemini_api_key") == "sk-1234567890abcdef"
    assert "sk-1234567890abcdef" not in str(await (await client.get("/api/settings")).json())
    actions = [(r.action, r.target, r.actor) for r in await services.store.audit_list(limit=2)]
    assert ("settings.update", "llm.gemini_api_key", "user:vince") in actions

    resp = await client.put("/api/settings", json={"controls.call_mode": "loud", "no.such": 1,
                                                   "controls.dnd": True}, headers=CSRF)
    assert resp.status == 400
    body = await resp.json()
    assert set(body["invalid"]) == {"controls.call_mode", "no.such"}
    assert services.config.source("controls.dnd") == "default"          # nothing written

    resp = await client.put("/api/settings", json={"controls.call_mode": None}, headers=CSRF)
    assert resp.status == 204 and services.config.source("controls.call_mode") == "default"
    resp = await client.put("/api/settings", json={"no.such": None}, headers=CSRF)
    assert resp.status == 400
    resp = await client.put("/api/settings", data=b"[]", headers={**CSRF, "Content-Type": "application/json"})
    assert resp.status == 400


# ---------------------------------------------------------------- tokens

async def test_tokens_lifecycle(client, services):
    await login(client)
    assert await (await client.get("/api/tokens")).json() == []
    resp = await client.post("/api/tokens", json={"name": "desktop"}, headers=CSRF)
    assert resp.status == 201
    created = await resp.json()
    assert created["name"] == "desktop" and created["token"].startswith("fn_")
    assert await status_as_node(client, "/nodes", created["token"]) == 200

    listed = await (await client.get("/api/tokens")).json()
    assert listed[0]["name"] == "desktop" and "token" not in listed[0] and listed[0]["revoked_at"] is None
    assert (await client.delete(f"/api/tokens/{created['id']}", headers=CSRF)).status == 204
    assert (await client.delete(f"/api/tokens/{created['id']}", headers=CSRF)).status == 404
    assert await status_as_node(client, "/nodes", created["token"]) == 401
    actions = [r.action for r in await services.store.audit_list(limit=3)]
    assert "token.create" in actions and "token.revoke" in actions
    assert (await client.post("/api/tokens", json={"name": ""}, headers=CSRF)).status == 400
    assert (await client.post("/api/tokens", json={"name": "x" * 65}, headers=CSRF)).status == 400


# ----------------------------------------------------------------- audit

async def test_audit_listing_and_paging(client, services):
    await login(client)
    for i in range(5):
        await services.store.audit_append(float(i), "test", "noop", str(i), {})
    page = await (await client.get("/api/audit?limit=3")).json()
    assert len(page) == 3 and page[0]["action"] == "noop" and page[0]["target"] == "4"
    older = await (await client.get(f"/api/audit?limit=10&before={page[-1]['id']}")).json()
    assert [r["target"] for r in older if r["action"] == "noop"] == ["1", "0"]
    assert (await client.get("/api/audit?limit=0")).status == 400
    assert (await client.get("/api/audit?before=x")).status == 400


# ---------------------------------------------------------------- config

async def test_config_pull(client, services, node_token):
    await services.config.set_many({"llm.gemini_api_key": "sk-abcdefghijklmnop"}, actor="test")
    resp = await client.get("/config?scope=desktop", headers=node_headers(node_token))
    assert resp.status == 200
    body = await resp.json()
    assert body["scope"] == "desktop" and body["values"]["llm.gemini_api_key"] == "sk-abcdefghijklmnop"
    assert "llm.routes.triage" not in body["values"] and body["generated_at"] > 0
    audit = (await services.store.audit_list())[0]
    assert audit.action == "config.pull" and audit.actor == "node:test-node" and audit.detail["scope"] == "desktop"

    assert (await client.get("/config?scope=desktop")).status == 401
    assert (await client.get("/config?scope=phone", headers=node_headers(node_token))).status == 400
    assert (await client.get("/config", headers=node_headers(node_token))).status == 400
    await login(client)
    assert (await client.get("/config?scope=desktop")).status == 200


# ----------------------------------------------------------------- shell

async def test_shell_and_static(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "login"
    resp = await client.get("/login")
    assert resp.status == 200 and "static/app.js" in await resp.text()
    resp = await client.get("/static/app.js")
    assert resp.status == 200 and "console.log" in await resp.text()
    await login(client)
    for path in ("/", "/settings", "/tokens"):
        resp = await client.get(path, allow_redirects=False)
        assert resp.status == 200 and "FRIDAY" in await resp.text()


async def test_ws_accepts_session_cookie(client, services):
    await login(client)
    ws = await client.ws_connect("/ws")
    await services.bus.publish(Event(type="a.b", source="s"))
    assert (await asyncio.wait_for(ws.receive_json(), 2))["type"] == "a.b"
    await ws.close()
