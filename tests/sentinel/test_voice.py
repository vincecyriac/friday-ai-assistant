import asyncio
import json
import time

import pytest

from friday.core.config import ConfigError
from friday.core.live import AudioOut, Interrupted, ToolFinished, ToolStarted, Transcript, TurnComplete
from friday.sentinel import services as services_module
from friday.sentinel import voice as voice_module
from friday.sentinel.auth import User
from friday.sentinel.voice import VoiceSession, WebSocketTransport

VINCE = User("vince")


class FakeWS:
    """Stands in for aiohttp's WebSocketResponse."""

    def __init__(self):
        self.binary = []
        self.json = []
        self.closed = False

    async def send_bytes(self, data):
        if self.closed:
            raise ConnectionResetError
        self.binary.append(bytes(data))

    async def send_json(self, obj):
        if self.closed:
            raise ConnectionResetError
        self.json.append(obj)


# ------------------------------------------------------------- live_client

async def test_live_client_uses_the_vault_key(services, monkeypatch):
    with pytest.raises(ConfigError):
        services.live_client()                       # nothing in env or vault: the real factory raises

    seen = []
    monkeypatch.setattr(services_module, "gemini_client", lambda settings: seen.append(settings) or "CLIENT")
    await services.config.set_many({"llm.gemini_api_key": "vault-key-0123456789"}, actor="t")
    assert services.live_client() == "CLIENT"
    assert seen[-1].gemini_api_key == "vault-key-0123456789"


async def test_live_model_comes_from_the_route(services):
    assert services.live_model() == "gemini-3.1-flash-live-preview"
    await services.config.set_many({"llm.routes.live": "gemini:custom-live"}, actor="t")
    assert services.live_model() == "custom-live"
    await services.config.set_many({"llm.routes.live": "openai:whatever"}, actor="t")
    with pytest.raises(ConfigError):
        services.live_model()


# --------------------------------------------------------------- transport

async def test_websocket_transport_round_trip():
    ws = FakeWS()
    t = WebSocketTransport(ws)
    await t.start()
    t.feed(b"mic")
    assert await t.read() == b"mic"
    await t.write(b"speech")
    assert ws.binary == [b"speech"]
    await t.clear()
    assert ws.json == [{"type": "clear"}]
    await t.stop()
    assert await t.read() is None


async def test_transport_write_after_close_is_silent():
    ws = FakeWS()
    ws.closed = True
    t = WebSocketTransport(ws)
    await t.write(b"x")                              # must not raise
    await t.clear()


# ------------------------------------------------------------ VoiceSession

class ScriptedLive:
    """Replaces LiveSession inside VoiceSession: emits scripted events."""

    instances = []

    def __init__(self, client, config, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.sent_text = []
        self.stopped = False
        self._queue = asyncio.Queue()
        self._stop = asyncio.Event()
        ScriptedLive.instances.append(self)

    def push(self, *events):
        for event in events:
            # The real LiveSession mirrors playback to its transport (see
            # tests/core/live/test_session_transport.py); the fake must too, or
            # VoiceSession looks broken when it is not.
            transport = self.kwargs.get("transport")
            if transport is not None:
                if isinstance(event, AudioOut):
                    asyncio.ensure_future(transport.write(event.pcm))
                elif isinstance(event, Interrupted):
                    asyncio.ensure_future(transport.clear())
            self._queue.put_nowait(event)

    async def run(self):
        await self._stop.wait()                      # like LiveSession: returns on stop()

    def stop(self):
        self.stopped = True
        self._stop.set()
        self._queue.put_nowait(None)

    async def events(self):
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def send_audio(self, pcm):
        pass

    async def send_text(self, text, *, turn_complete=True):
        self.sent_text.append(text)


@pytest.fixture
def scripted(monkeypatch, services):
    ScriptedLive.instances = []
    monkeypatch.setattr(voice_module, "LiveSession", ScriptedLive)
    monkeypatch.setattr(services_module, "gemini_client", lambda settings: "CLIENT")
    return ScriptedLive


async def _session(services, ws, conversation_id="c1"):
    if await services.store.conversation_get(conversation_id) is None:
        await services.store.conversation_create(conversation_id, "New conversation", ts=1.0)
    return VoiceSession(services, VINCE, conversation_id, ws)


async def test_config_is_built_from_the_vault(services, scripted):
    await services.config.set_many({"voice.name": "Kore"}, actor="t")
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    session.stop()
    await asyncio.wait_for(task, 2)

    config = scripted.instances[0].config
    assert config.model == "gemini-3.1-flash-live-preview" and config.voice == "Kore"
    assert config.input_transcription is True and config.output_transcription is True
    assert {t["name"] for t in config.tools} == {"get_nodes", "get_telemetry", "get_queue",
                                                 "get_recent_events", "get_audit", "get_settings",
                                                 "set_controls"}
    assert "sentinel-test" in config.system_instruction


async def test_events_reach_the_browser(services, scripted):
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.02)
    live = scripted.instances[0]
    live.push(AudioOut(pcm=b"speech"),
              Transcript(text="partial", role="assistant", final=False),
              ToolStarted(name="get_nodes", args={}),
              ToolFinished(name="get_nodes", output="[]", ms=4))
    await asyncio.sleep(0.05)
    session.stop()
    await asyncio.wait_for(task, 2)

    kinds = [m["type"] for m in ws.json]
    assert "transcript" in kinds and kinds.count("tool") == 2
    tool_frames = [m for m in ws.json if m["type"] == "tool"]
    assert tool_frames[0]["phase"] == "start" and tool_frames[1]["output"] == "[]"


async def test_final_transcripts_are_persisted_with_via_voice(services, scripted):
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.02)
    live = scripted.instances[0]
    live.push(Transcript(text="how are ", role="user", final=False),
              Transcript(text="you?", role="user", final=True),          # fragments accumulate
              Transcript(text="All good.", role="assistant", final=True),
              TurnComplete())
    await asyncio.sleep(0.05)
    session.stop()
    await asyncio.wait_for(task, 2)

    rows = await services.store.messages_list("c1")
    assert [(r.role, r.content, r.via) for r in rows] == [
        ("user", "how are you?", "voice"), ("assistant", "All good.", "voice")]
    assert (await services.store.conversation_get("c1")).title == "how are you?"


async def test_turn_complete_flushes_an_unfinished_transcript(services, scripted):
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.02)
    live = scripted.instances[0]
    live.push(Transcript(text="never marked final", role="user", final=False), TurnComplete())
    await asyncio.sleep(0.05)
    session.stop()
    await asyncio.wait_for(task, 2)

    rows = await services.store.messages_list("c1")
    assert [(r.role, r.content) for r in rows] == [("user", "never marked final")]


async def test_typed_text_is_forwarded(services, scripted):
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.02)
    await session.feed_text("mute calls")
    await asyncio.sleep(0.02)
    session.stop()
    await asyncio.wait_for(task, 2)
    assert scripted.instances[0].sent_text == ["mute calls"]


async def test_audit_records_the_session(services, scripted):
    ws = FakeWS()
    session = await _session(services, ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.02)
    session.stop()
    await asyncio.wait_for(task, 2)
    entries = [r for r in await services.store.audit_list() if r.action == "voice.session"]
    assert len(entries) == 1
    assert entries[0].actor == "user:vince" and entries[0].detail["conversation_id"] == "c1"
    assert entries[0].detail["seconds"] >= 0


# ------------------------------------------------------------- /voice/ws

import aiohttp

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
async def client(aiohttp_client, services, scripted):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    c = await aiohttp_client(create_app(services))
    assert (await c.post("/auth/login", json={"username": "vince", "password": "pw"},
                         headers=CSRF)).status == 204
    return c


async def test_voice_ws_requires_a_principal(aiohttp_client, services, scripted):
    anon = await aiohttp_client(create_app(services))
    resp = await anon.get("/voice/ws")
    assert resp.status == 401


async def test_voice_ws_is_refused_when_disabled(client, services):
    await services.config.set_many({"voice.enabled": False}, actor="t")
    resp = await client.get("/voice/ws")
    assert resp.status == 503 and (await resp.json())["error"] == "voice is disabled"


async def test_voice_ws_streams_audio_both_ways(client, services, scripted):
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    ws = await client.ws_connect(f"/voice/ws?conversation={conv['id']}")
    await asyncio.sleep(0.05)
    live = scripted.instances[0]

    await ws.send_bytes(b"mic-chunk")
    await asyncio.sleep(0.05)
    live.push(AudioOut(pcm=b"model-speech"))
    frames = []
    while len(frames) < 1:
        frame = await asyncio.wait_for(ws.receive(), 2)
        if frame.type is aiohttp.WSMsgType.BINARY:
            frames.append(frame.data)
    assert frames == [b"model-speech"]
    await ws.close()
    await asyncio.sleep(0.05)
    assert live.stopped is True


async def test_voice_ws_forwards_typed_text_and_closes_cleanly(client, services, scripted):
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    ws = await client.ws_connect(f"/voice/ws?conversation={conv['id']}")
    await asyncio.sleep(0.05)
    await ws.send_json({"type": "text", "content": "mute calls"})
    await asyncio.sleep(0.05)
    assert scripted.instances[0].sent_text == ["mute calls"]
    await ws.close()


async def test_second_socket_for_one_conversation_is_refused(client, services, scripted):
    conv = await (await client.post("/api/chat", json={}, headers=CSRF)).json()
    first = await client.ws_connect(f"/voice/ws?conversation={conv['id']}")
    await asyncio.sleep(0.05)
    resp = await client.get(f"/voice/ws?conversation={conv['id']}")
    assert resp.status == 409
    await first.close()


async def test_unknown_conversation_gets_a_new_one(client, services, scripted):
    ws = await client.ws_connect("/voice/ws")
    await asyncio.sleep(0.05)
    assert len(await services.store.conversations_list()) == 1
    await ws.close()


async def test_missing_key_closes_with_an_error(client, services, monkeypatch):
    def no_client():
        raise ConfigError("GEMINI_API_KEY is not set")
    monkeypatch.setattr(services, "live_client", no_client)
    ws = await client.ws_connect("/voice/ws")
    body = None
    while body is None:
        frame = await asyncio.wait_for(ws.receive(), 2)
        if frame.type is aiohttp.WSMsgType.TEXT:
            payload = json.loads(frame.data)
            if payload["type"] == "error":
                body = payload
    assert "Settings" in body["message"]
    await ws.close()
