import asyncio
import json
import time

import pytest

from friday.core.llm import Chunk, ToolCall
from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password

CSRF = {"X-FRIDAY-Client": "dashboard"}
END = Chunk("end")


class ScriptedProvider:
    name = model = "scripted"

    def __init__(self, *steps, delay_s=0.0):
        self.steps = list(steps)
        self.delay_s = delay_s

    async def stream(self, messages, *, system=None, tools=None, temperature=None, timeout_s=60.0):
        for chunk in self.steps.pop(0):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield chunk


@pytest.fixture
async def client(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    c = await aiohttp_client(create_app(services))
    assert (await c.post("/auth/login", json={"username": "vince", "password": "pw"}, headers=CSRF)).status == 204
    return c


async def _lines(resp):
    out = []
    async for raw in resp.content:
        if raw.strip():
            out.append(json.loads(raw))
    return out


async def test_conversation_crud(client):
    assert await (await client.get("/api/chat")).json() == []
    resp = await client.post("/api/chat", json={}, headers=CSRF)
    assert resp.status == 201
    conv = await resp.json()
    assert conv["title"] == "New conversation" and conv["message_count"] == 0 and len(conv["id"]) == 16
    resp = await client.post("/api/chat", json={"title": "  Ops  "}, headers=CSRF)
    assert (await resp.json())["title"] == "Ops"
    listed = await (await client.get("/api/chat")).json()
    assert [c["title"] for c in listed] == ["Ops", "New conversation"]
    body = await (await client.get(f"/api/chat/{conv['id']}")).json()
    assert body["conversation"]["id"] == conv["id"] and body["messages"] == []
    assert (await client.get("/api/chat/nope")).status == 404
    assert (await client.delete(f"/api/chat/{conv['id']}", headers=CSRF)).status == 204
    assert (await client.delete(f"/api/chat/{conv['id']}", headers=CSRF)).status == 404
    assert (await client.post("/api/chat", json={})).status == 403                  # CSRF
    assert (await client.delete("/api/chat/x")).status == 403


async def test_message_stream_and_persistence(client, services):
    services.provider_for = lambda role: ScriptedProvider(
        [Chunk("text", text="Hi "), Chunk("tool_call", tool_call=ToolCall("get_queue", {})), END],
        [Chunk("text", text="all quiet."), END])
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    resp = await client.post(f"/api/chat/{conv['id']}/messages", json={"content": "status?"}, headers=CSRF)
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("application/x-ndjson")
    assert resp.headers["Cache-Control"] == "no-store"
    lines = await _lines(resp)
    assert [l["type"] for l in lines] == ["delta", "tool", "result", "delta", "done"]
    assert lines[0]["text"] == "Hi " and lines[1]["name"] == "get_queue" and "pending" in json.loads(lines[2]["output"])
    body = await (await client.get(f"/api/chat/{conv['id']}")).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert body["messages"][2]["tool_args"] == {} and body["messages"][-1]["id"] == lines[-1]["message_id"]
    assert {m["via"] for m in body["messages"]} == {"text"}
    assert body["conversation"]["title"] == "status?" and body["conversation"]["message_count"] == 4


async def test_message_validation(client):
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    url = f"/api/chat/{conv['id']}/messages"
    assert (await client.post(url, json={"content": ""}, headers=CSRF)).status == 400
    assert (await client.post(url, json={"content": "x" * 8001}, headers=CSRF)).status == 400
    assert (await client.post(url, data=b"nope", headers={**CSRF, "Content-Type": "application/json"})).status == 400
    assert (await client.post("/api/chat/nope/messages", json={"content": "x"}, headers=CSRF)).status == 404
    assert (await client.post(url, json={"content": "x"})).status == 403


async def test_concurrent_turn_is_409(client, services):
    services.provider_for = lambda role: ScriptedProvider([Chunk("text", text="slow"), END], delay_s=0.3)
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    url = f"/api/chat/{conv['id']}/messages"
    first = asyncio.create_task(client.post(url, json={"content": "a"}, headers=CSRF))
    await asyncio.sleep(0.1)
    second = await client.post(url, json={"content": "b"}, headers=CSRF)
    assert second.status == 409 and (await second.json())["error"] == "a turn is in progress"
    resp = await first
    assert resp.status == 200 and [l["type"] for l in await _lines(resp)] == ["delta", "done"]
    again = await client.post(url, json={"content": "c"}, headers=CSRF)      # lock released
    assert again.status == 200


async def test_missing_key_streams_error(client, services):
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    resp = await client.post(f"/api/chat/{conv['id']}/messages", json={"content": "hi"}, headers=CSRF)
    lines = await _lines(resp)
    assert lines[0]["type"] == "error" and "Gemini" in lines[0]["message"] and lines[1]["type"] == "done"


async def test_chat_routes_require_session(aiohttp_client, services, node_token):
    anon = await aiohttp_client(create_app(services))
    assert (await anon.get("/api/chat")).status == 401
    assert (await anon.get("/api/chat", headers={"Authorization": f"Bearer {node_token}"})).status == 401
