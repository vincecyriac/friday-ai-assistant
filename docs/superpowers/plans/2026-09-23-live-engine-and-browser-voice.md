# Live Engine Extraction and Browser Voice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Gemini Live session out of `friday/desktop/hub.py` into a tested `friday.core.live` package with a typed event stream and a pluggable audio transport; make the hub its first consumer with behaviour unchanged; and give the dashboard a voice assistant — the sentinel's own Live session over a binary `/voice/ws`, the ported FRIDAY orb, and the same seven tools the text assistant uses.

**Architecture:** `friday/core/live/` owns connect/resume/rotate/backoff, message decoding into events, and tool dispatch; consumers react to `session.events()` and push media with `send_audio/send_video/send_text`. The hub passes no transport and keeps its PyAudio multiplexing; the sentinel passes a `WebSocketTransport` and persists transcripts into the existing conversation. Three.js and `orb.js` move to a shared `friday/webassets/` package mounted at `/shared` by both nodes.

**Tech Stack:** Python ≥ 3.11, `google-genai` 2.12 (`client.aio.live.connect`, `LiveConnectConfig`, `AudioTranscriptionConfig`, `SessionResumptionConfig`), `aiohttp` WebSockets (binary frames), stdlib `sqlite3`; vanilla ES modules + Three.js in the browser; `pytest` + `pytest-aiohttp`; `node` (optional, syntax checks only).

**Spec:** `docs/superpowers/specs/2026-09-23-live-engine-and-browser-voice-design.md` (roadmap: `docs/superpowers/specs/2026-09-21-sentinel-evolution-roadmap.md`; sub-projects 1 and 2 specs in the same directory)

## Global Constraints

- **No commits.** The user's global rule. Leave all work in the working tree on the current branch.
- Python `>=3.11`; no 3.12+ syntax. `asyncio.timeout` is allowed.
- `friday.core` and `friday.sentinel` never import `Quartz`, `pyaudio`, `cv2`, `webview`, `EventKit`, `Foundation`, `AppKit`, `objc`, `pyautogui`, `termios`, `tty`, `PIL`, `numpy`. `tests/test_boundaries.py` must stay green after every task. `friday.core.live` adds **no** new dependency — `google-genai` only.
- The desktop keeps its current behaviour exactly: 38 tools, widgets, senses, phone mirroring, status/broadcast wiring, in-memory session handle. `hub.py` loses code, never capability.
- Secrets never appear in logs, events, audit detail or API responses.
- SQLite stays WAL; migrations forward-only; multi-statement changes use explicit `BEGIN`/`COMMIT`.
- Dashboard: relative URLs only, class strings literal (never concatenated) so `test_css_covers_every_class` can see them, `deploy/build_css.sh` rerun whenever a class changes.
- `transparent=` is never set on `SessionResumptionConfig`: it is Vertex-only and the Developer API refuses the whole connection when present.
- Use `.venv/bin/python` / `.venv/bin/pytest` for everything.
- Names in a task's **Interfaces → Produces** block are contracts for later tasks; keep them exact.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `friday/core/live/__init__.py` | re-exports every public name |
| `friday/core/live/events.py` | the ten event dataclasses + `LiveEvent` union |
| `friday/core/live/transport.py` | `AudioTransport` protocol, `QueueTransport` |
| `friday/core/live/session.py` | `LiveConfig`, `ToolHandler`, `LiveSession` |
| `friday/webassets/__init__.py` | `WEBASSETS_DIR` |
| `friday/webassets/orb.js`, `three.module.min.js` | moved from `friday/desktop/web_gui/` |
| `friday/sentinel/voice.py` | `WebSocketTransport`, `VoiceSession` |
| `friday/sentinel/dashboard/views/voice.js` | mic capture, playback, `/voice/ws` client, orb feed |
| `tests/core/live/conftest.py` | `FakeLiveClient` and message builders |
| `tests/core/live/test_events.py`, `test_transport.py`, `test_session.py`, `test_session_transport.py` | core tests |
| `tests/desktop/test_hub_live.py` | the hub's event reactions |
| `tests/sentinel/test_voice.py` | `VoiceSession`, `/voice/ws`, transcripts |
| `tests/core/test_storage_v4.py` | the `via` column |

**Modified**

`friday/desktop/hub.py`, `friday/desktop/web_gui/index.html`, `friday/core/storage.py`, `friday/sentinel/settings_registry.py`, `friday/sentinel/services.py`, `friday/sentinel/web.py`, `friday/sentinel/dashboard/views/assistant.js`, `friday/sentinel/dashboard/index.html`, `friday/sentinel/dashboard/tailwind.src.css`, `friday/sentinel/dashboard/tailwind.css`, `pyproject.toml`, `.env.template`, `deploy/README.md`, `readme.md`, `tests/core/test_storage.py`, `tests/core/test_storage_v2.py`, `tests/core/test_storage_v3.py`, `tests/sentinel/test_settings_registry.py`, `tests/sentinel/test_dashboard_files.py`, `tests/sentinel/test_chat_api.py`.

---

### Task 1: Events and transport

**Files:**
- Create: `friday/core/live/__init__.py`, `friday/core/live/events.py`, `friday/core/live/transport.py`
- Test: `tests/core/live/__init__.py` (empty), `tests/core/live/test_events.py`, `tests/core/live/test_transport.py`

**Interfaces:**
- Produces in `friday.core.live.events`: frozen dataclasses `Connected(resumed: bool)`, `Disconnected(reason: str, will_retry: bool)`, `AudioOut(pcm: bytes)`, `TextOut(text: str)`, `Transcript(text: str, role: str, final: bool)`, `Interrupted()`, `TurnComplete()`, `ToolStarted(name: str, args: dict)`, `ToolFinished(name: str, output: str, ms: int, failed: bool)`, `GoAway(time_left: float | None)`; `LiveEvent` union alias.
- Produces in `friday.core.live.transport`: `class AudioTransport(Protocol)` with `async start()`, `async read() -> bytes | None`, `async write(pcm: bytes) -> None`, `async clear() -> None`, `async stop() -> None`; `class QueueTransport` implementing it with `feed(pcm)`, `feed_eof()`, `written: list[bytes]`, `clears: int`, `started: bool`, `stopped: bool`.
- Produces in `friday.core.live`: every name above re-exported, plus `LiveConfig`, `LiveSession`, `ToolHandler` (Task 2).

- [ ] **Step 1: Write the failing tests**

`tests/core/live/test_events.py`:
```python
from friday.core.live import (AudioOut, Connected, Disconnected, GoAway, Interrupted, TextOut,
                              ToolFinished, ToolStarted, Transcript, TurnComplete)


def test_events_are_frozen_value_objects():
    assert Connected(resumed=True) == Connected(resumed=True)
    assert AudioOut(pcm=b"ab").pcm == b"ab"
    assert TextOut(text="hi").text == "hi"
    assert Transcript(text="yes", role="user", final=True).final is True
    assert Interrupted() == Interrupted() and TurnComplete() == TurnComplete()
    assert ToolStarted(name="t", args={"a": 1}).args == {"a": 1}
    assert ToolFinished(name="t", output="o", ms=5, failed=False).failed is False
    assert GoAway(time_left=None).time_left is None
    assert Disconnected(reason="closed", will_retry=False).will_retry is False
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        Connected(resumed=True).resumed = False


import pytest
```

`tests/core/live/test_transport.py`:
```python
import asyncio

from friday.core.live import QueueTransport


async def test_queue_transport_round_trip():
    t = QueueTransport()
    await t.start()
    assert t.started is True
    t.feed(b"one")
    t.feed(b"two")
    assert await t.read() == b"one"
    assert await t.read() == b"two"
    await t.write(b"out")
    assert t.written == [b"out"]
    await t.clear()
    assert t.clears == 1
    t.feed_eof()
    assert await t.read() is None
    await t.stop()
    assert t.stopped is True


async def test_read_waits_for_data():
    t = QueueTransport()
    task = asyncio.create_task(t.read())
    await asyncio.sleep(0.01)
    assert not task.done()
    t.feed(b"late")
    assert await asyncio.wait_for(task, 1) == b"late"


async def test_stop_unblocks_a_pending_read():
    t = QueueTransport()
    task = asyncio.create_task(t.read())
    await asyncio.sleep(0.01)
    await t.stop()
    assert await asyncio.wait_for(task, 1) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/live -q`
Expected: `ModuleNotFoundError: No module named 'friday.core.live'`.

- [ ] **Step 3: Implement**

`friday/core/live/events.py`:
```python
"""What a Live session tells its consumer. One frozen value object per thing
that can happen, so the hub, the dashboard and (later) the call agent all react
to the same vocabulary instead of decoding Gemini's wire messages themselves."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class Connected:
    resumed: bool


@dataclass(frozen=True)
class Disconnected:
    reason: str
    will_retry: bool


@dataclass(frozen=True)
class AudioOut:
    pcm: bytes                       # 24 kHz mono PCM16 from the model


@dataclass(frozen=True)
class TextOut:
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    role: str                        # "user" | "assistant"
    final: bool


@dataclass(frozen=True)
class Interrupted:
    pass


@dataclass(frozen=True)
class TurnComplete:
    pass


@dataclass(frozen=True)
class ToolStarted:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolFinished:
    name: str
    output: str
    ms: int
    failed: bool = False


@dataclass(frozen=True)
class GoAway:
    time_left: float | None = None


LiveEvent = Union[Connected, Disconnected, AudioOut, TextOut, Transcript, Interrupted,
                  TurnComplete, ToolStarted, ToolFinished, GoAway]
```

`friday/core/live/transport.py`:
```python
"""Where a session's audio comes from and goes to.

A consumer that wants the session to pump audio for it passes a transport; one
that multiplexes its own audio (the desktop hub) passes none and calls
``send_audio`` itself.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioTransport(Protocol):
    async def start(self) -> None: ...
    async def read(self) -> bytes | None: ...      # 16 kHz mic PCM16; None ends the pump
    async def write(self, pcm: bytes) -> None: ...  # 24 kHz model PCM16
    async def clear(self) -> None: ...             # drop buffered playback (barge-in)
    async def stop(self) -> None: ...


class QueueTransport:
    """In-memory transport: tests and any caller happy to push and pull queues."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.written: list[bytes] = []
        self.clears = 0
        self.started = False
        self.stopped = False

    # ------------------------------------------------ producer side (caller)

    def feed(self, pcm: bytes) -> None:
        self._inbound.put_nowait(pcm)

    def feed_eof(self) -> None:
        self._inbound.put_nowait(None)

    # ---------------------------------------------- AudioTransport (session)

    async def start(self) -> None:
        self.started = True

    async def read(self) -> bytes | None:
        return await self._inbound.get()

    async def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    async def clear(self) -> None:
        self.clears += 1

    async def stop(self) -> None:
        self.stopped = True
        self._inbound.put_nowait(None)          # unblock a pending read
```

`friday/core/live/__init__.py`:
```python
"""Gemini Live sessions, decoupled from any one node's audio plumbing."""

from friday.core.live.events import (AudioOut, Connected, Disconnected, GoAway, Interrupted,
                                     LiveEvent, TextOut, ToolFinished, ToolStarted, Transcript,
                                     TurnComplete)
from friday.core.live.transport import AudioTransport, QueueTransport

__all__ = ["AudioOut", "AudioTransport", "Connected", "Disconnected", "GoAway", "Interrupted",
           "LiveEvent", "QueueTransport", "TextOut", "ToolFinished", "ToolStarted", "Transcript",
           "TurnComplete"]
```

Create an empty `tests/core/live/__init__.py`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/live -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 2: `LiveSession` — connect loop, decoding, tool dispatch

**Files:**
- Create: `friday/core/live/session.py`
- Modify: `friday/core/live/__init__.py`
- Test: `tests/core/live/conftest.py`, `tests/core/live/test_session.py`

**Interfaces:**
- Consumes: the events and `AudioTransport` from Task 1.
- Produces: `@dataclass(frozen=True) LiveConfig(model: str, voice: str = "Aoede", system_instruction: str = "", tools: tuple[Mapping[str, Any], ...] = (), google_search: bool = False, input_transcription: bool = False, output_transcription: bool = False, resume_handle: str | None = None, max_backoff_s: float = 10.0)`; `ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bytes | None]]]`; `class LiveSession(client, config, *, tool_handler=None, transport=None, on_handle=None, logger=None)` with `state: str`, `async run()`, `stop()`, `async send_audio(pcm)`, `async send_video(jpeg)`, `async send_text(text, *, turn_complete=True)`, `events() -> AsyncIterator[LiveEvent]`.
- Produces in `tests/core/live/conftest.py`: `FakeLiveClient(turns=(), fail_times=0, fail_exc=None)` with `.connects: list[dict]`, `.sent_audio/.sent_video/.sent_text/.tool_responses: list`, `.close_current()`, `.push_turn(messages)`; helpers `audio_msg(pcm)`, `text_msg(text)`, `interrupt_msg()`, `turn_complete_msg()`, `tool_msg(name, args, id="c1")`, `handle_msg(handle)`, `goaway_msg(time_left=5.0)`, `transcript_msg(*, inp=None, out=None, final=True)`.

- [ ] **Step 1: Write the fake Live client**

`tests/core/live/conftest.py`:
```python
"""A scripted stand-in for google-genai's live session.

The real ``session.receive()`` yields ONE conversational turn and then ends;
the session object stays usable and the caller calls receive() again. The fake
models exactly that: each scripted "turn" is a list of messages, and once the
script is exhausted receive() blocks until the connection is closed.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from google.genai import types


# ------------------------------------------------------------ message builders

def _content(**kw):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


def audio_msg(pcm: bytes):
    return _content(model_turn=types.Content(role="model", parts=[
        types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))]))


def text_msg(text: str):
    return _content(model_turn=types.Content(role="model", parts=[types.Part(text=text)]))


def interrupt_msg():
    return _content(interrupted=True)


def turn_complete_msg():
    return _content(turn_complete=True)


def transcript_msg(*, inp=None, out=None, final=True):
    kw = {}
    if inp is not None:
        kw["input_transcription"] = types.Transcription(text=inp, finished=final)
    if out is not None:
        kw["output_transcription"] = types.Transcription(text=out, finished=final)
    return _content(**kw)


def tool_msg(name: str, args: dict, id: str = "c1"):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=id, name=name, args=args)]))


def tools_msg(*calls):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=f"c{i}", name=n, args=a)
                        for i, (n, a) in enumerate(calls)]))


def handle_msg(handle: str, resumable: bool = True):
    return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(
        new_handle=handle, resumable=resumable))


def goaway_msg(time_left: float | None = 5.0):
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=time_left))


# ------------------------------------------------------------------- the fake

class FakeSession:
    def __init__(self, client):
        self._client = client
        self._closed = asyncio.Event()

    async def receive(self):
        if self._client.turns:
            for message in self._client.turns.pop(0):
                if isinstance(message, Exception):
                    raise message
                yield message
            return                                   # one turn, then the caller loops
        await self._closed.wait()                    # idle session: stay open

    def close_now(self):
        self._closed.set()

    async def send_realtime_input(self, *, audio=None, video=None, text=None, **kw):
        if audio is not None:
            self._client.sent_audio.append(bytes(audio.data))
        if video is not None:
            self._client.sent_video.append(bytes(video.data))
        if text is not None:
            self._client.sent_text.append(text)

    async def send_client_content(self, *, turns=None, turn_complete=True):
        self._client.client_content.append((turns, turn_complete))

    async def send_tool_response(self, *, function_responses):
        self._client.tool_responses.append(list(function_responses))


class FakeLiveClient:
    """Stands in for ``client`` — only ``client.aio.live.connect`` is used."""

    def __init__(self, turns=(), *, fail_times: int = 0, fail_exc: Exception | None = None):
        self.turns = [list(t) for t in turns]
        self.fail_times = fail_times
        self.fail_exc = fail_exc or RuntimeError("connect refused")
        self.connects: list[dict] = []
        self.sent_audio: list[bytes] = []
        self.sent_video: list[bytes] = []
        self.sent_text: list[str] = []
        self.client_content: list = []
        self.tool_responses: list = []
        self.current: FakeSession | None = None
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

    @asynccontextmanager
    async def _connect(self, *, model, config):
        self.connects.append({"model": model, "config": config})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exc
        session = FakeSession(self)
        self.current = session
        try:
            yield session
        finally:
            self.current = None

    def push_turn(self, messages):
        self.turns.append(list(messages))
        if self.current is not None:
            self.current.close_now()        # let the idle receive() return so the turn is read

    def close_current(self):
        if self.current is not None:
            self.current.close_now()
```

- [ ] **Step 2: Write the failing session tests**

`tests/core/live/test_session.py`:
```python
import asyncio

import pytest
from google.genai import types

from friday.core.live import (AudioOut, Connected, Disconnected, GoAway, Interrupted, LiveConfig,
                              LiveSession, TextOut, ToolFinished, ToolStarted, Transcript,
                              TurnComplete)
from tests.core.live.conftest import (FakeLiveClient, audio_msg, goaway_msg, handle_msg,
                                      interrupt_msg, text_msg, tool_msg, tools_msg,
                                      transcript_msg, turn_complete_msg)

CONFIG = LiveConfig(model="m", voice="Kore", system_instruction="be brief")


async def drain(session, count, timeout=2.0):
    """The first `count` events, then stop the session."""
    out = []

    async def collect():
        async for event in session.events():
            out.append(event)
            if len(out) >= count:
                return
    try:
        await asyncio.wait_for(collect(), timeout)
    finally:
        session.stop()
    return out


async def run_until(client, config=CONFIG, *, count=1, timeout=2.0, **kwargs):
    session = LiveSession(client, config, **kwargs)
    task = asyncio.create_task(session.run())
    try:
        events = await drain(session, count, timeout)
    finally:
        session.stop()
        await asyncio.wait_for(task, 2)
    return session, events


# ------------------------------------------------------------------ connect

async def test_connect_emits_connected_and_builds_config():
    client = FakeLiveClient([[text_msg("hi")]])
    _, events = await run_until(client, count=2)
    assert events[0] == Connected(resumed=False)
    assert events[1] == TextOut(text="hi")

    config = client.connects[0]["config"]
    assert client.connects[0]["model"] == "m"
    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert config.system_instruction.parts[0].text == "be brief"
    assert config.session_resumption.handle is None
    assert config.session_resumption.transparent is None      # Vertex-only; must stay unset
    assert config.tools is None
    assert config.input_audio_transcription is None and config.output_audio_transcription is None


async def test_config_carries_tools_search_transcription_and_handle():
    config = LiveConfig(model="m", tools=({"name": "t", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}},),
                        google_search=True, input_transcription=True, output_transcription=True,
                        resume_handle="h-1")
    client = FakeLiveClient([[text_msg("x")]])
    _, events = await run_until(client, config, count=1)
    assert events[0] == Connected(resumed=True)
    sent = client.connects[0]["config"]
    assert [t.function_declarations[0].name for t in sent.tools if t.function_declarations] == ["t"]
    assert any(t.google_search is not None for t in sent.tools)
    assert sent.session_resumption.handle == "h-1"
    assert sent.input_audio_transcription is not None and sent.output_audio_transcription is not None


# ------------------------------------------------------------------ decoding

async def test_audio_text_turn_and_interrupt():
    client = FakeLiveClient([[audio_msg(b"pcm"), text_msg("said"), turn_complete_msg()],
                             [interrupt_msg()]])
    _, events = await run_until(client, count=5)
    assert events[1:] == [AudioOut(pcm=b"pcm"), TextOut(text="said"), TurnComplete(), Interrupted()]


async def test_transcripts_carry_role_and_final():
    client = FakeLiveClient([[transcript_msg(inp="how are you", final=False),
                              transcript_msg(inp="how are you?", final=True),
                              transcript_msg(out="All good.", final=True)]])
    _, events = await run_until(client, count=4)
    assert events[1:] == [Transcript(text="how are you", role="user", final=False),
                          Transcript(text="how are you?", role="user", final=True),
                          Transcript(text="All good.", role="assistant", final=True)]


async def test_receive_loops_across_turns_without_reconnecting():
    client = FakeLiveClient([[text_msg("one")], [text_msg("two")]])
    _, events = await run_until(client, count=3)
    assert [e for e in events if isinstance(e, TextOut)] == [TextOut(text="one"), TextOut(text="two")]
    assert len(client.connects) == 1                 # one connection served both turns


async def test_handle_updates_are_reported():
    seen = []
    client = FakeLiveClient([[handle_msg("h-new"), text_msg("x")]])
    await run_until(client, count=2, on_handle=seen.append)
    assert seen == ["h-new"]


async def test_non_resumable_handle_is_ignored():
    seen = []
    client = FakeLiveClient([[handle_msg("h", resumable=False), text_msg("x")]])
    await run_until(client, count=2, on_handle=seen.append)
    assert seen == []


async def test_goaway_rotates_the_session():
    client = FakeLiveClient([[handle_msg("h-1"), goaway_msg(3.0)], [text_msg("after")]])
    _, events = await run_until(client, count=5)
    kinds = [type(e).__name__ for e in events]
    assert kinds[:4] == ["Connected", "GoAway", "Disconnected", "Connected"]
    assert events[1] == GoAway(time_left=3.0)
    assert events[3] == Connected(resumed=True)                  # the handle was carried over
    assert client.connects[1]["config"].session_resumption.handle == "h-1"


# --------------------------------------------------------------- tool calls

async def test_tool_call_is_executed_and_answered():
    calls = []

    async def handler(name, args):
        calls.append((name, args))
        return "result text", None

    client = FakeLiveClient([[tool_msg("get_nodes", {"a": 1})]])
    _, events = await run_until(client, count=3, tool_handler=handler)
    assert calls == [("get_nodes", {"a": 1})]
    assert events[1] == ToolStarted(name="get_nodes", args={"a": 1})
    finished = events[2]
    assert isinstance(finished, ToolFinished) and finished.output == "result text" and finished.failed is False
    responses = client.tool_responses[0]
    assert len(responses) == 1 and responses[0].name == "get_nodes"
    assert responses[0].response == {"output": "result text"} and responses[0].id == "c1"


async def test_tool_image_is_sent_back_as_video():
    async def handler(name, args):
        return "looked", b"\xff\xd8jpeg"

    client = FakeLiveClient([[tool_msg("look_at_screen", {})]])
    await run_until(client, count=3, tool_handler=handler)
    assert client.sent_video == [b"\xff\xd8jpeg"]


async def test_two_calls_in_one_batch_get_one_response_message():
    async def handler(name, args):
        return f"{name} done", None

    client = FakeLiveClient([[tools_msg(("a_tool", {}), ("b_tool", {"x": 2}))]])
    _, events = await run_until(client, count=5, tool_handler=handler)
    assert [type(e).__name__ for e in events[1:]] == ["ToolStarted", "ToolFinished",
                                                      "ToolStarted", "ToolFinished"]
    assert [r.name for r in client.tool_responses[0]] == ["a_tool", "b_tool"]


async def test_a_raising_tool_still_answers_the_turn():
    async def handler(name, args):
        raise RuntimeError("kaput")

    client = FakeLiveClient([[tool_msg("boom", {})]])
    _, events = await run_until(client, count=3, tool_handler=handler)
    finished = events[2]
    assert finished.failed is True and "boom failed: kaput" in finished.output
    assert client.tool_responses[0][0].response["output"].startswith("[Tool error]")


async def test_missing_handler_still_answers():
    client = FakeLiveClient([[tool_msg("whatever", {})]])
    _, events = await run_until(client, count=3)
    assert "no tool handler" in events[2].output
    assert client.tool_responses[0][0].response["output"] == events[2].output


# ------------------------------------------------------------------ sending

async def test_sends_reach_the_session_and_are_noops_before_connect():
    client = FakeLiveClient([[text_msg("x")]])
    session = LiveSession(client, CONFIG)
    await session.send_audio(b"early")                 # no session yet: dropped, no raise
    assert client.sent_audio == []

    task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(_first(session), 2)
        await session.send_audio(b"mic")
        await session.send_video(b"jpg")
        await session.send_text("typed")
        await session.send_text("partial", turn_complete=False)
        assert client.sent_audio == [b"mic"] and client.sent_video == [b"jpg"]
        assert client.client_content[0][1] is True and client.client_content[1][1] is False
        assert client.client_content[0][0][0]["parts"][0]["text"] == "typed"
    finally:
        session.stop()
        await asyncio.wait_for(task, 2)


async def _first(session):
    async for _ in session.events():
        return


# ------------------------------------------------------- failure and backoff

async def test_connect_failure_retries_with_backoff_then_succeeds(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("friday.core.live.session.asyncio.sleep", fake_sleep)
    client = FakeLiveClient([[text_msg("finally")]], fail_times=3)
    _, events = await run_until(client, count=4)
    assert [type(e).__name__ for e in events] == ["Disconnected", "Disconnected", "Disconnected", "Connected"]
    assert all(e.will_retry for e in events[:3])
    assert slept[:3] == [2, 4, 8]


async def test_backoff_is_capped():
    config = LiveConfig(model="m", max_backoff_s=3.0)
    client = FakeLiveClient([[text_msg("ok")]], fail_times=1)
    session = LiveSession(client, config)
    assert session._backoff(9) == 3.0                    # 2**3 capped to max_backoff_s
    assert session._backoff(1) == 2.0


async def test_refused_resume_clears_the_handle_but_a_live_drop_keeps_it():
    # 1. resume refused at the handshake → handle dropped, next connect is cold
    client = FakeLiveClient([[text_msg("cold")]], fail_times=1)
    _, events = await run_until(client, LiveConfig(model="m", resume_handle="stale"), count=2)
    assert client.connects[0]["config"].session_resumption.handle == "stale"
    assert client.connects[1]["config"].session_resumption.handle is None
    assert isinstance(events[1], Connected) and events[1].resumed is False

    # 2. an established session that drops keeps its handle for the resume
    client = FakeLiveClient([[handle_msg("h-live"), RuntimeError("socket died")], [text_msg("back")]])
    _, events = await run_until(client, count=4)
    assert client.connects[1]["config"].session_resumption.handle == "h-live"


async def test_stop_ends_run_promptly_and_closes_events():
    client = FakeLiveClient([[text_msg("x")]])
    session = LiveSession(client, CONFIG)
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(_first(session), 2)
    session.stop()
    await asyncio.wait_for(task, 2)
    assert session.state == "closed"
    assert [e async for e in session.events()] == []      # the stream is finished


async def test_state_transitions():
    client = FakeLiveClient([[text_msg("x")]])
    session = LiveSession(client, CONFIG)
    assert session.state == "idle"
    task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(_first(session), 2)
        assert session.state == "live"
    finally:
        session.stop()
        await asyncio.wait_for(task, 2)
    assert session.state == "closed"
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/live/test_session.py -q`
Expected: `ImportError: cannot import name 'LiveConfig'`.

- [ ] **Step 4: Implement**

`friday/core/live/session.py`:
```python
"""A Gemini Live session that survives rotations, answers tool calls, and reports
everything as typed events.

Three behaviours here were expensive to learn and are the reason this lives in
one place:

* ``session.receive()`` yields ONE conversational turn and then ends. Without the
  outer loop the session is torn down after the first reply, and the resumed
  handle replays that turn — re-firing its tool calls on every rotation.
* A tool that raises must still produce a tool response. Skipping it leaves the
  turn open forever and the model re-issues the same calls after every resume.
* A resumption handle is dropped only when the handshake itself was refused
  while resuming. A live session that drops keeps its handle — that handle is
  exactly what resumes the conversation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from google.genai import types

from friday.core.live.events import (AudioOut, Connected, Disconnected, GoAway, Interrupted,
                                     LiveEvent, TextOut, ToolFinished, ToolStarted, Transcript,
                                     TurnComplete)
from friday.core.live.transport import AudioTransport

log = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bytes | None]]]


@dataclass(frozen=True)
class LiveConfig:
    model: str
    voice: str = "Aoede"
    system_instruction: str = ""
    tools: tuple[Mapping[str, Any], ...] = ()
    google_search: bool = False
    input_transcription: bool = False
    output_transcription: bool = False
    resume_handle: str | None = None
    max_backoff_s: float = 10.0


class LiveSession:
    def __init__(self, client: Any, config: LiveConfig, *, tool_handler: ToolHandler | None = None,
                 transport: AudioTransport | None = None,
                 on_handle: Callable[[str], None] | None = None,
                 logger: logging.Logger | None = None) -> None:
        self._client = client
        self._config = config
        self._tool_handler = tool_handler
        self._transport = transport
        self._on_handle = on_handle
        self._log = logger or log
        self._events: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._session: Any = None
        self._handle = config.resume_handle
        self.state = "idle"

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Connect, serve, rotate — until ``stop()``. A connect failure is an
        event and a retry, never an exception out of here."""
        failures = 0
        if self._transport is not None:
            await self._transport.start()
        try:
            while not self._stop.is_set():
                resuming = self._handle is not None
                self.state = "reconnecting" if resuming else "connecting"
                established = False
                try:
                    async with self._client.aio.live.connect(
                            model=self._config.model, config=self._connect_config()) as session:
                        established = True
                        self._session = session
                        self.state = "live"
                        failures = 0
                        self._emit(Connected(resumed=resuming))
                        await self._serve(session)
                    self._emit(Disconnected(reason="session ended", will_retry=not self._stop.is_set()))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failures += 1
                    retry = not self._stop.is_set()
                    self._emit(Disconnected(reason=str(e), will_retry=retry))
                    if resuming and not established:
                        # The handshake itself was refused: the handle is stale.
                        self._log.info("live: resume refused (%s); starting fresh", e)
                        self._handle = None
                        if retry:
                            await asyncio.sleep(0.5)
                        continue
                    self._log.warning("live: session error (%s)", e)
                    if retry:
                        await asyncio.sleep(self._backoff(failures))
                    continue
                finally:
                    self._session = None
                if not self._stop.is_set():
                    await asyncio.sleep(0.2)          # brief pause before rotating
        finally:
            self.state = "closed"
            self._session = None
            if self._transport is not None:
                try:
                    await self._transport.stop()
                except Exception:
                    pass
            self._events.put_nowait(None)

    def stop(self) -> None:
        self._stop.set()

    def _backoff(self, failures: int) -> float:
        return min(self._config.max_backoff_s, float(2 ** min(failures, 3)))

    async def _serve(self, session: Any) -> None:
        """Run the receive loop (and the transport pumps) until the session ends
        or ``stop()`` is called."""
        disconnected = asyncio.Event()
        tasks = [asyncio.create_task(self._receive_loop(session, disconnected))]
        tasks.extend(self._pump_tasks(disconnected))
        waiters = [asyncio.create_task(self._stop.wait()),
                   asyncio.create_task(disconnected.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            disconnected.set()
            for task in (*waiters, *tasks):
                task.cancel()
            await asyncio.gather(*waiters, *tasks, return_exceptions=True)

    def _pump_tasks(self, disconnected: asyncio.Event) -> list[asyncio.Task]:
        return []                                     # Task 3 adds the transport pumps

    # ---------------------------------------------------------------- config

    def _connect_config(self) -> types.LiveConnectConfig:
        c = self._config
        tools: list[types.Tool] = []
        if c.google_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        if c.tools:
            tools.append(types.Tool(function_declarations=[
                types.FunctionDeclaration(**dict(t)) for t in c.tools]))
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=c.voice))),
            system_instruction=types.Content(parts=[types.Part.from_text(text=c.system_instruction)]),
            tools=tools or None,
            input_audio_transcription=types.AudioTranscriptionConfig() if c.input_transcription else None,
            output_audio_transcription=types.AudioTranscriptionConfig() if c.output_transcription else None,
            # No "transparent": that flag is Vertex only, and the Developer API
            # refuses the whole connection when it is set. Sent on every connect
            # (handle=None simply starts a fresh resumable session) because this
            # config is what asks the server for SessionResumptionUpdates.
            session_resumption=types.SessionResumptionConfig(handle=self._handle),
        )

    # ---------------------------------------------------------------- events

    def _emit(self, event: LiveEvent) -> None:
        self._events.put_nowait(event)

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Every event until the session closes. One consumer is expected."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    # --------------------------------------------------------------- sending

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(lambda s: s.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")), "audio")

    async def send_video(self, jpeg: bytes) -> None:
        await self._send(lambda s: s.send_realtime_input(
            video=types.Blob(data=jpeg, mime_type="image/jpeg")), "video")

    async def send_text(self, text: str, *, turn_complete: bool = True) -> None:
        await self._send(lambda s: s.send_client_content(
            turns=[{"role": "user", "parts": [{"text": text}]}], turn_complete=turn_complete), "text")

    async def _send(self, call: Callable[[Any], Awaitable[None]], what: str) -> None:
        session = self._session
        if session is None:
            self._log.debug("live: dropped %s, no session", what)
            return
        try:
            await call(session)
        except Exception as e:
            self._log.debug("live: could not send %s: %s", what, e)

    # -------------------------------------------------------------- decoding

    async def _receive_loop(self, session: Any, disconnected: asyncio.Event) -> None:
        try:
            while not (self._stop.is_set() or disconnected.is_set()):
                async for message in session.receive():
                    if self._stop.is_set() or disconnected.is_set():
                        return
                    if await self._handle_message(session, message):
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.info("live: receive ended (%s)", e)
        finally:
            disconnected.set()

    async def _handle_message(self, session: Any, message: Any) -> bool:
        """Decode one server message. Returns True when the session must rotate."""
        if message.go_away is not None:
            self._emit(GoAway(time_left=getattr(message.go_away, "time_left", None)))
            return True

        content = message.server_content
        if content is not None:
            if content.interrupted:
                self._emit(Interrupted())
                return False
            for part in (content.model_turn.parts if content.model_turn else []) or []:
                if part.inline_data is not None and part.inline_data.data:
                    self._emit(AudioOut(pcm=part.inline_data.data))
                if part.text:
                    self._emit(TextOut(text=part.text))
            self._emit_transcript(content.input_transcription, "user")
            self._emit_transcript(content.output_transcription, "assistant")
            if content.turn_complete:
                self._emit(TurnComplete())

        update = message.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self._handle = update.new_handle
            if self._on_handle is not None:
                self._on_handle(update.new_handle)

        if message.tool_call is not None:
            await self._run_tools(session, message.tool_call.function_calls or [])
        return False

    def _emit_transcript(self, transcription: Any, role: str) -> None:
        if transcription is None or not transcription.text:
            return
        self._emit(Transcript(text=transcription.text, role=role,
                              final=bool(getattr(transcription, "finished", False))))

    async def _run_tools(self, session: Any, calls: list[Any]) -> None:
        responses = []
        for call in calls:
            name, args = call.name, dict(call.args or {})
            self._emit(ToolStarted(name=name, args=args))
            started = time.monotonic()
            image = None
            failed = False
            if self._tool_handler is None:
                output = f"[Tool error] {name}: no tool handler configured"
                failed = True
            else:
                try:
                    output, image = await self._tool_handler(name, args)
                except Exception as e:
                    output, failed = f"[Tool error] {name} failed: {e}", True
                    self._log.info("live: %s", output)
            output = str(output)
            if image:
                await self._send(lambda s, img=image: s.send_realtime_input(
                    video=types.Blob(data=img, mime_type="image/jpeg")), "tool image")
            self._emit(ToolFinished(name=name, output=output,
                                    ms=int((time.monotonic() - started) * 1000), failed=failed))
            responses.append(types.FunctionResponse(id=call.id, name=name,
                                                    response={"output": output}))
        if responses:
            # A tool that raised still answers: an unanswered turn never closes
            # and the model re-issues the call after every resume.
            try:
                await session.send_tool_response(function_responses=responses)
            except Exception as e:
                self._log.info("live: could not send tool response: %s", e)
```

Extend `friday/core/live/__init__.py`:
```python
from friday.core.live.events import (AudioOut, Connected, Disconnected, GoAway, Interrupted,
                                     LiveEvent, TextOut, ToolFinished, ToolStarted, Transcript,
                                     TurnComplete)
from friday.core.live.session import LiveConfig, LiveSession, ToolHandler
from friday.core.live.transport import AudioTransport, QueueTransport

__all__ = ["AudioOut", "AudioTransport", "Connected", "Disconnected", "GoAway", "Interrupted",
           "LiveConfig", "LiveEvent", "LiveSession", "QueueTransport", "TextOut", "ToolFinished",
           "ToolHandler", "ToolStarted", "Transcript", "TurnComplete"]
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/live -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS. If `test_goaway_rotates_the_session` hangs, the fake's idle `receive()` is
not being released — check that `push_turn` closes the current session.

---

### Task 3: Transport pumps

**Files:**
- Modify: `friday/core/live/session.py` (`_pump_tasks`)
- Test: `tests/core/live/test_session_transport.py`

**Interfaces:**
- Consumes: `QueueTransport`, `LiveSession` from Tasks 1–2.
- Produces: `LiveSession` with a transport pumps mic → `send_audio` and `AudioOut` → `transport.write`, calls `transport.clear()` on `Interrupted`, and `start()`/`stop()`s it around `run()`. `events()` still yields everything; the transport is additive.

- [ ] **Step 1: Write the failing tests**

`tests/core/live/test_session_transport.py`:
```python
import asyncio

from friday.core.live import AudioOut, Connected, Interrupted, LiveConfig, LiveSession, QueueTransport
from tests.core.live.conftest import FakeLiveClient, audio_msg, interrupt_msg, text_msg

CONFIG = LiveConfig(model="m")


async def _run(client, transport, *, until, timeout=2.0):
    session = LiveSession(client, CONFIG, transport=transport)
    task = asyncio.create_task(session.run())
    consumer = asyncio.create_task(_consume(session))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not until() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        session.stop()
        await asyncio.wait_for(task, 2)
        consumer.cancel()
    return session


async def _consume(session):
    async for _ in session.events():
        pass


async def test_transport_is_started_and_stopped():
    transport = QueueTransport()
    client = FakeLiveClient([[text_msg("x")]])
    await _run(client, transport, until=lambda: transport.started)
    assert transport.started is True and transport.stopped is True


async def test_mic_audio_is_pumped_into_the_session():
    transport = QueueTransport()
    client = FakeLiveClient([[text_msg("x")]])
    transport.feed(b"chunk-1")
    transport.feed(b"chunk-2")
    await _run(client, transport, until=lambda: len(client.sent_audio) >= 2)
    assert client.sent_audio == [b"chunk-1", b"chunk-2"]


async def test_model_audio_is_written_to_the_transport():
    transport = QueueTransport()
    client = FakeLiveClient([[audio_msg(b"spoken")]])
    await _run(client, transport, until=lambda: transport.written)
    assert transport.written == [b"spoken"]


async def test_interrupt_clears_buffered_playback():
    transport = QueueTransport()
    client = FakeLiveClient([[audio_msg(b"a"), interrupt_msg()]])
    await _run(client, transport, until=lambda: transport.clears)
    assert transport.clears == 1


async def test_eof_from_the_transport_ends_the_pump_not_the_session():
    transport = QueueTransport()
    client = FakeLiveClient([[text_msg("x")]])
    transport.feed(b"last")
    transport.feed_eof()
    session = await _run(client, transport, until=lambda: client.sent_audio == [b"last"])
    assert client.sent_audio == [b"last"]
    assert session.state == "closed"        # only because _run stopped it
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/live/test_session_transport.py -q`
Expected: FAIL — nothing is pumped (`_pump_tasks` returns `[]`).

- [ ] **Step 3: Implement**

In `friday/core/live/session.py` replace `_pump_tasks` and add the two pumps. `_emit` also
notifies the transport, so playback and barge-in need no consumer cooperation:
```python
    def _pump_tasks(self, disconnected: asyncio.Event) -> list[asyncio.Task]:
        if self._transport is None:
            return []
        return [asyncio.create_task(self._mic_pump(disconnected))]

    async def _mic_pump(self, disconnected: asyncio.Event) -> None:
        """Transport → model. Ends on EOF or disconnect; never ends the session."""
        transport = self._transport
        assert transport is not None
        try:
            while not (self._stop.is_set() or disconnected.is_set()):
                chunk = await transport.read()
                if chunk is None:
                    return
                await self.send_audio(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.info("live: mic pump ended (%s)", e)
```
and in `_emit`, mirror playback-relevant events to the transport:
```python
    def _emit(self, event: LiveEvent) -> None:
        self._events.put_nowait(event)
        if self._transport is None:
            return
        if isinstance(event, AudioOut):
            self._spawn(self._transport.write(event.pcm))
        elif isinstance(event, Interrupted):
            self._spawn(self._transport.clear())

    def _spawn(self, coro) -> None:
        """Fire-and-forget on the running loop; a transport write must never
        block decoding or raise into it."""
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
```
Add `self._pending: set[asyncio.Task] = set()` in `__init__`, and in `run()`'s `finally`, before
stopping the transport:
```python
            for task in list(self._pending):
                task.cancel()
            if self._pending:
                await asyncio.gather(*self._pending, return_exceptions=True)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/core/live -q && .venv/bin/pytest tests/test_boundaries.py -q`
Expected: all PASS.

---

### Task 4: The hub delegates to `LiveSession`

**Files:**
- Modify: `friday/desktop/hub.py`
- Test: `tests/desktop/test_hub_live.py`

**Interfaces:**
- Consumes: `LiveConfig`, `LiveSession` and every event from Tasks 1–3.
- Produces in `hub.py`: `mic_pump_task(session, input_stream)`, `live_event_task(session)`,
  `stream_senses_task(session)` (one argument now), `global_live_session` holding a
  `LiveSession` for the whole run. `receive_audio_task` and `run_session_tasks` are gone.
- Behaviour is unchanged: the same broadcasts, the same statuses, the same play queue rules.

**Read first:** this is a refactor of the user's daily driver. Change only what the steps say.
If a step's `old` text is not found verbatim, stop and report rather than improvising.

- [ ] **Step 1: Write the failing test**

`tests/desktop/test_hub_live.py`:
```python
"""The hub's reactions to Live events. macOS only (imports hub)."""
import asyncio

import pytest

pytest.importorskip("Quartz", reason="desktop extra not installed")

from friday.core.live import (AudioOut, Connected, Disconnected, Interrupted, TextOut,
                              ToolFinished, ToolStarted, TurnComplete)


class FakeSession:
    """Feeds scripted events to hub.live_event_task."""

    def __init__(self, events):
        self._events = list(events)

    async def events(self):
        for event in self._events:
            yield event


@pytest.fixture
def captured(monkeypatch):
    from friday.desktop import hub

    events = []
    monkeypatch.setattr(hub, "broadcast_event", events.append)
    monkeypatch.setattr(hub, "log_interaction", lambda *a, **k: None)
    while not hub.play_queue.empty():
        hub.play_queue.get_nowait()
    return events


async def test_audio_out_is_queued_and_broadcast(captured):
    from friday.desktop import hub

    await hub.live_event_task(FakeSession([AudioOut(pcm=b"\x01\x02"), TurnComplete()]))
    assert hub.play_queue.get_nowait() == b"\x01\x02"
    kinds = [e["type"] for e in captured]
    assert "audio_out" in kinds and "status" in kinds
    assert hub.current_system_status == "Listening"


async def test_text_out_is_logged_to_the_chat(captured):
    from friday.desktop import hub

    await hub.live_event_task(FakeSession([TextOut(text="on it")]))
    chat = [e for e in captured if e["type"] == "chat_log"]
    assert chat and chat[-1]["sender"] == "FRIDAY" and chat[-1]["text"] == "on it"


async def test_interrupt_drains_the_play_queue_and_announces(captured):
    from friday.desktop import hub

    hub.play_queue.put_nowait(b"stale")
    await hub.live_event_task(FakeSession([Interrupted()]))
    assert hub.play_queue.empty()
    assert any(e["type"] == "interrupted" for e in captured)
    assert hub.model_is_speaking is False and hub.model_turn_active is False


async def test_tool_events_become_the_two_activity_broadcasts(captured):
    from friday.desktop import hub

    await hub.live_event_task(FakeSession([
        ToolStarted(name="look_at_screen", args={"a": 1}),
        ToolFinished(name="look_at_screen", output="a screenshot", ms=12)]))
    activity = [e for e in captured if e["type"] == "tool_activity"]
    assert [a["phase"] for a in activity] == ["start", "done"]
    assert activity[0]["name"] == "look_at_screen" and '"a": 1' in activity[0]["args_preview"]
    assert activity[1]["result_preview"] == "a screenshot"


async def test_connection_events_set_status(captured):
    from friday.desktop import hub

    await hub.live_event_task(FakeSession([Connected(resumed=False)]))
    assert hub.current_system_status == "Listening"
    await hub.live_event_task(FakeSession([Disconnected(reason="socket died", will_retry=True)]))
    assert hub.current_system_status == "Connection Failed"


async def test_mic_pump_sends_only_when_no_browser_client(monkeypatch):
    from friday.desktop import hub

    sent = []

    class Session:
        async def send_audio(self, pcm):
            sent.append(pcm)

    class Stream:
        def __init__(self):
            self.reads = 0

        def read(self, n, exception_on_overflow=False):
            self.reads += 1
            if self.reads > 2:
                hub.shutdown_event.set()
            return b"mic"

    monkeypatch.setattr(hub, "shutdown_event", asyncio.Event())
    monkeypatch.setattr(hub, "model_is_speaking", False)
    monkeypatch.setattr(hub, "connected_ws_clients", set())
    await asyncio.wait_for(hub.mic_pump_task(Session(), Stream()), 5)
    assert sent and all(chunk == b"mic" for chunk in sent)

    monkeypatch.setattr(hub, "shutdown_event", asyncio.Event())
    monkeypatch.setattr(hub, "connected_ws_clients", {object()})
    sent.clear()
    await asyncio.wait_for(hub.mic_pump_task(Session(), Stream()), 5)
    assert sent == []                      # a browser client owns the mic
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/desktop/test_hub_live.py -q`
Expected: `AttributeError: module 'friday.desktop.hub' has no attribute 'live_event_task'`.

- [ ] **Step 3: Add the core import**

In `friday/desktop/hub.py`, after `from friday.core.llm import gemini_client, resolve`:
```python
from friday.core.live import (AudioOut, Connected, Disconnected, GoAway, Interrupted, LiveConfig,
                              LiveSession, TextOut, ToolFinished, ToolStarted, TurnComplete)
```

- [ ] **Step 4: Replace `send_audio_task` with `mic_pump_task`**

Replace the whole `async def send_audio_task(session, input_stream, session_disconnect_event):`
function (it ends at the blank line before `async def stream_senses_task`) with:
```python
async def mic_pump_task(session, input_stream):
    """Mac microphone → Live session, for the whole run (the session survives
    rotations now, so this is no longer per-connection)."""
    global mic_audio_buffer
    loop = asyncio.get_running_loop()
    while not shutdown_event.is_set():
        try:
            data = await loop.run_in_executor(
                None,
                lambda: input_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            )
            if data:
                mic_audio_buffer.extend(data)
                if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                    mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]

                # Only send PyAudio mic data to Gemini if NO WebSocket GUI client is connected
                if not model_is_speaking and not connected_ws_clients:
                    await session.send_audio(data)

        except asyncio.CancelledError:
            break
        except Exception as e:
            if not shutdown_event.is_set():
                log_info(f"Error reading mic: {e}")
            await asyncio.sleep(0.1)
```

- [ ] **Step 5: Simplify `stream_senses_task`**

Three edits inside `async def stream_senses_task(session, session_disconnect_event):`:
1. the signature becomes `async def stream_senses_task(session):`
2. both `while not shutdown_event.is_set() and not session_disconnect_event.is_set():` and the
   two `if not session_disconnect_event.is_set() and not shutdown_event.is_set():` guards drop
   the `session_disconnect_event` half — they become `while not shutdown_event.is_set():` and
   `if not shutdown_event.is_set():`
3. both
   ```python
                        await session.send_realtime_input(
                            video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                        )
   ```
   and the identical `webcam_bytes` call become `await session.send_video(screen_bytes)` and
   `await session.send_video(webcam_bytes)`.

- [ ] **Step 6: Replace `receive_audio_task` with `live_event_task`**

Delete the whole `async def receive_audio_task(session, session_disconnect_event):` function
(from its `def` line to the blank line before `IMAGE_MAX_BYTES = 6 * 1024 * 1024`) and put in
its place:
```python
async def live_event_task(session):
    """Everything the Live session reports, turned into the GUI's reality:
    playback, chat log, status, interruption, tool activity."""
    global model_is_speaking, model_turn_active
    async for event in session.events():
        try:
            if isinstance(event, AudioOut):
                set_system_status("Speaking")
                model_turn_active = True
                await play_queue.put(event.pcm)
                broadcast_event({"type": "audio_out",
                                 "pcm_base64": base64.b64encode(event.pcm).decode("utf-8")})

            elif isinstance(event, TextOut):
                broadcast_event({"type": "chat_log", "sender": "FRIDAY",
                                 "text": event.text, "style": "friday"})

            elif isinstance(event, Interrupted):
                set_system_status("Listening (Interrupted)")
                interrupted_event.set()
                model_is_speaking = False
                while not play_queue.empty():
                    try:
                        play_queue.get_nowait()
                        play_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                await asyncio.sleep(0.05)
                interrupted_event.clear()
                model_turn_active = False
                broadcast_event({"type": "interrupted"})
                log_interaction("user_interruption", {})

            elif isinstance(event, TurnComplete):
                model_turn_active = False
                set_system_status("Listening")

            elif isinstance(event, ToolStarted):
                set_system_status(f"Executing {event.name}")
                log_info(f"Tool call: {event.name}")
                broadcast_event({"type": "tool_activity", "phase": "start", "name": event.name,
                                 "args_preview": json.dumps(event.args)[:220]})
                log_interaction("tool_call_received", {"name": event.name, "args": event.args})

            elif isinstance(event, ToolFinished):
                log_info(f"Result: {event.output[:50]}...")
                broadcast_event({"type": "tool_activity", "phase": "done", "name": event.name,
                                 "result_preview": event.output[:300]})
                log_interaction("tool_call_executed",
                                {"name": event.name, "output_preview": event.output[:100]})

            elif isinstance(event, Connected):
                set_system_status("Listening")
                log_interaction("connection_success", {"resumed": event.resumed})

            elif isinstance(event, Disconnected):
                if event.will_retry:
                    set_system_status("Connection Failed")
                log_info(f"Live session ended: {event.reason}")
                log_interaction("connection_error", {"error": event.reason})

            elif isinstance(event, GoAway):
                log_info(f"Received GoAway from Gemini API (time left: {event.time_left}). Rotating...")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_info(f"Error handling live event: {e}")
```

- [ ] **Step 7: Delete `run_session_tasks`**

Delete the whole `async def run_session_tasks(session, input_stream):` function (from its `def`
line to the blank line before `def build_system_instruction(memory_str: str) -> str:`).

- [ ] **Step 8: Migrate the other `global_live_session` call sites**

Four edits, each a send that now goes through the session's own API:
1. in `notify_session_remote_change`:
   ```python
        await global_live_session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": note}]}],
            turn_complete=False
        )
   ```
   → `await global_live_session.send_text(note, turn_complete=False)`
2. in the `remote_camera_frame` branch of `ws_handler`:
   ```python
                            await global_live_session.send_realtime_input(
                                video=types.Blob(data=latest_remote_frame_bytes, mime_type="image/jpeg")
                            )
   ```
   → `await global_live_session.send_video(latest_remote_frame_bytes)`
3. in the `audio_in` branch:
   ```python
                            await global_live_session.send_realtime_input(
                                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                            )
   ```
   → `await global_live_session.send_audio(pcm_bytes)`
4. in the `user_text` branch:
   ```python
                        await global_live_session.send_client_content(
                            turns=[{"role": "user", "parts": [{"text": text}]}],
                            turn_complete=True
                        )
   ```
   → `await global_live_session.send_text(text)`
5. in the `sve_user_action` branch:
   ```python
                                await global_live_session.send_client_content(
                                    turns=[{"role": "user", "parts": [{"text":
                                        f"[UI context, not a question — do not respond yet: Vince is now pointing at {note}. "
                                        "If his next question says 'this' or 'it', he means that object.]"}]}],
                                    turn_complete=False
   ```
   (plus its closing `)`) → 
   ```python
                                await global_live_session.send_text(
                                    f"[UI context, not a question — do not respond yet: Vince is now pointing at {note}. "
                                    "If his next question says 'this' or 'it', he means that object.]",
                                    turn_complete=False)
   ```
6. in `agent_result_dispatcher`:
   ```python
            await global_live_session.send_client_content(
                turns=[{"role": "user", "parts": [{"text":
                    f"[Background {label} agent finished. Tell Vince this result now, in one short "
                    f"spoken sentence, without mentioning agents or tools: {outcome}]"}]}],
                turn_complete=True,
            )
   ```
   →
   ```python
            await global_live_session.send_text(
                f"[Background {label} agent finished. Tell Vince this result now, in one short "
                f"spoken sentence, without mentioning agents or tools: {outcome}]")
   ```

`live_is_idle()` and the `if not global_live_session:` guards are unchanged.

- [ ] **Step 9: Rewrite the connect block in `run_friday`**

Replace everything from `    consecutive_failures = 0` down to (and including) the line
`                await sleep_unless_shutdown(0.2)` — that is, the `clear_session_handle()`
comment block, the `try:`/`while` connect loop and its `except`/rotate tail, but **not** the
`finally:` that follows — with:
```python
    # New process, new conversation: the handle starts empty and is only
    # populated by this run's own session, for rotations within it.
    clear_session_handle()

    global global_live_session
    live_session = LiveSession(
        client,
        LiveConfig(model=MODEL_ID, voice=LIVE_VOICE,
                   system_instruction=system_instruction_text,
                   tools=tuple(TOOL_FUNCTION_DECLARATIONS), google_search=True,
                   resume_handle=current_session_handle),
        tool_handler=execute_tool,
        on_handle=save_session_handle,
    )
    global_live_session = live_session
    set_system_status("Connecting to API")
    log_interaction("connection_attempt", {"model": MODEL_ID, "resuming": False})

    mic_task = asyncio.create_task(mic_pump_task(live_session, input_stream))
    senses_task = asyncio.create_task(stream_senses_task(live_session))
    events_task = asyncio.create_task(live_event_task(live_session))
    stop_waiter = asyncio.create_task(shutdown_event.wait())

    try:
        session_runner = asyncio.create_task(live_session.run())
        await asyncio.wait([session_runner, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        live_session.stop()
        await asyncio.wait_for(session_runner, timeout=5)
    except asyncio.TimeoutError:
        log_info("Live session did not stop in time; continuing shutdown.")
    finally:
```
and immediately inside that existing `finally:` block, before `set_system_status("Shutting Down")`,
add:
```python
        live_session.stop()
        global_live_session = None
        for task in (mic_task, senses_task, events_task, stop_waiter):
            task.cancel()
        await asyncio.gather(mic_task, senses_task, events_task, stop_waiter, return_exceptions=True)
```

- [ ] **Step 10: Run the tests**

Run: `.venv/bin/pytest tests/desktop -q && .venv/bin/pytest -q`
Expected: all PASS.

Run: `.venv/bin/python -c "import ast,sys; ast.parse(open('friday/desktop/hub.py').read())" && grep -n "receive_audio_task\|run_session_tasks\|send_audio_task\|session_disconnect_event" friday/desktop/hub.py`
Expected: no output from the grep — every trace of the old plumbing is gone.

- [ ] **Step 11: Manual desktop pass (do not skip)**

```bash
.venv/bin/python -m friday.desktop.hub      # or .venv/bin/python -m friday.desktop
```
Then, in order:
1. say "hello" — FRIDAY answers in voice; the orb goes through listening → speaking.
2. ask a long question and **interrupt** it mid-sentence — the voice stops immediately and the
   status shows `Listening (Interrupted)`.
3. say "look at my screen and tell me what you see" — the tool fires (`tool_activity` in the
   GUI), the screenshot reaches the model, and the answer describes the screen.
4. say "show me Apple stock" — a widget appears and hydrates.
5. leave it running ~10 minutes until a rotation happens (`GoAway` in the log) and confirm the
   conversation continues without repeating the last turn's tool calls.
6. open `http://127.0.0.1:8766` in a browser and confirm the phone/browser mic path still
   drives the session (the Mac mic goes quiet while the page is connected).
7. Ctrl+C — the engine prints `Project FRIDAY engine terminated. Goodbye.` and exits cleanly.

If any step regresses, stop: this task is revertible on its own (`git checkout friday/desktop/hub.py`),
because Tasks 1–3 are purely additive.

---

### Task 5: Shared web assets and `mountOrb`

**Files:**
- Create: `friday/webassets/__init__.py`; move `friday/desktop/web_gui/vendor/three.module.min.js` → `friday/webassets/three.module.min.js` and `friday/desktop/web_gui/orb.js` → `friday/webassets/orb.js`
- Modify: `friday/webassets/orb.js` (mount API), `friday/desktop/web_gui/index.html`, `friday/desktop/hub.py` (static route), `friday/sentinel/web.py` (static route), `pyproject.toml`
- Test: `tests/test_webassets.py`, `tests/sentinel/test_dashboard_files.py` (extend)

**Interfaces:**
- Produces: `friday.webassets.WEBASSETS_DIR: Path`; `orb.js` exporting `mountOrb(element)` and
  still setting `window.FridayOrb = {setState, setLevel, resize, state}`; both nodes serving the
  directory at `/shared` (hub: `http://127.0.0.1:8766/shared/...`; sentinel: `/shared/...`).

- [ ] **Step 1: Write the failing tests**

`tests/test_webassets.py`:
```python
import re

from friday.webassets import WEBASSETS_DIR


def test_shared_assets_exist():
    assert (WEBASSETS_DIR / "three.module.min.js").is_file()
    orb = WEBASSETS_DIR / "orb.js"
    assert orb.is_file()
    src = orb.read_text()
    assert "export function mountOrb" in src
    assert "window.FridayOrb" in src
    assert 'import * as THREE from "three"' in src


def test_desktop_gui_points_at_the_shared_copy():
    from friday.desktop import hub          # noqa: F401  (path only; no Quartz needed)
    import friday.desktop as desktop
    from pathlib import Path

    gui = Path(desktop.__file__).parent / "web_gui"
    assert not (gui / "orb.js").exists()
    assert not (gui / "vendor" / "three.module.min.js").exists()
    html = (gui / "index.html").read_text()
    assert '"three": "./shared/three.module.min.js"' in html
    assert 'src="shared/orb.js"' in html
```
(If importing `hub` fails without the desktop extra the file check still stands — drop the
import line and keep the path assertions.)

In `tests/sentinel/test_dashboard_files.py`, extend the served-paths tuple in
`test_real_dashboard_is_served` with `"/shared/orb.js", "/shared/three.module.min.js"`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_webassets.py -q`
Expected: `ModuleNotFoundError: No module named 'friday.webassets'`.

- [ ] **Step 3: Move the files**

```bash
mkdir -p friday/webassets
git mv friday/desktop/web_gui/orb.js friday/webassets/orb.js
git mv friday/desktop/web_gui/vendor/three.module.min.js friday/webassets/three.module.min.js
```

`friday/webassets/__init__.py`:
```python
"""Browser assets both nodes serve: the FRIDAY orb and its Three.js runtime.

The desktop HUD mounts this at /shared from its GUI server; the sentinel mounts
the same directory at /shared from the dashboard app. One orb, one three.js.
"""

from pathlib import Path

WEBASSETS_DIR = Path(__file__).resolve().parent

__all__ = ["WEBASSETS_DIR"]
```

- [ ] **Step 4: Give the orb a mount API**

In `friday/webassets/orb.js`:
1. replace `const stage = document.getElementById("orb-stage");` with
   ```js
   let stage = null;
   ```
2. the file ends with `init();` — replace that with:
   ```js
   /** Mount the orb into `element`. Called by the sentinel dashboard; the desktop
    *  HUD keeps its zero-argument auto-mount below. */
   export function mountOrb(element) {
     if (!element) return null;
     stage = element;
     init();
     return window.FridayOrb;
   }

   const autoStage = document.getElementById("orb-stage");
   if (autoStage) mountOrb(autoStage);
   ```
3. `init()` and `resize()` already read the module-level `stage`; no other change is needed.
   Verify with `grep -n "stage" friday/webassets/orb.js` that every use is inside a function.

- [ ] **Step 5: Point the desktop GUI at `/shared`**

In `friday/desktop/web_gui/index.html`:
```html
    { "imports": { "three": "./vendor/three.module.min.js" } }
```
→
```html
    { "imports": { "three": "./shared/three.module.min.js" } }
```
An import-map address MUST be an absolute URL or start with `/`, `./` or `../`. A bare
`"shared/…"` is dropped by the browser, and every `import … from "three"` then fails — which
kills the orb, the SVE and the asset viewer at once.

and
```html
  <script type="module" src="orb.js"></script>
```
→
```html
  <script type="module" src="shared/orb.js"></script>
```

In `friday/desktop/hub.py`'s `start_gui_server`, add the mount **before** the catch-all
`app.router.add_static("/", path=gui_dir)` (aiohttp matches in registration order):
```python
    from friday.webassets import WEBASSETS_DIR
    app.router.add_static("/shared", path=str(WEBASSETS_DIR))
```

- [ ] **Step 6: Serve it from the sentinel too**

In `friday/sentinel/web.py`, import the directory next to `DASHBOARD_DIR`:
```python
from friday.webassets import WEBASSETS_DIR
```
and in `add_web_routes`, beside the existing static mount:
```python
    if static_dir.is_dir():
        app.router.add_static("/static", static_dir, show_index=False)
    if WEBASSETS_DIR.is_dir():
        app.router.add_static("/shared", WEBASSETS_DIR, show_index=False)
```

In `pyproject.toml` add the package data:
```toml
"friday.webassets" = ["*.js"]
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/test_webassets.py tests/sentinel/test_dashboard_files.py tests/desktop -q && .venv/bin/pytest -q`
Expected: all PASS.

Manual: start the hub, open `http://127.0.0.1:8766`, confirm the orb still renders and reacts
(this is the one thing the tests cannot see).

---

### Task 6: Storage v4 (`via`) and the `voice` settings group

**Files:**
- Modify: `friday/core/storage.py`, `friday/sentinel/settings_registry.py`, `friday/sentinel/web.py`, `.env.template`
- Test: `tests/core/test_storage_v4.py`; update `tests/core/test_storage.py`, `tests/core/test_storage_v2.py`, `tests/core/test_storage_v3.py`, `tests/sentinel/test_settings_registry.py`, `tests/sentinel/test_chat_api.py`

**Interfaces:**
- Produces: `MessageRow.via: str` (last field); `Store.message_append(..., via: str = "text")` and
  its async twin; `_message_dict` includes `"via"`; schema version 4.
- Produces registry keys `voice.enabled` (bool, group `voice`, default `True`) and `voice.name`
  (enum `Aoede|Kore|Charon|Fenrir|Puck`, group `voice`, default `Aoede`);
  `GROUP_ORDER == ("llm", "desktop", "sentinel", "voice", "controls")`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_storage_v4.py`:
```python
import pytest

from friday.core import storage
from friday.core.storage import AsyncStore, Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v3_database_upgrades_to_v4_and_defaults_via(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {k: v for k, v in storage.MIGRATIONS.items() if k <= 3})
    old = Store.open(path)
    old.conversation_create("c1", "t", ts=1.0)
    old.message_append("c1", "user", "hello", ts=2.0)
    assert old.schema_version() == 3
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 4
        rows = s.messages_list("c1")
        assert len(rows) == 1 and rows[0].content == "hello" and rows[0].via == "text"
    finally:
        s.close()


def test_via_round_trips(store):
    store.conversation_create("c", "t", ts=1.0)
    text = store.message_append("c", "user", "typed", ts=2.0)
    voice = store.message_append("c", "user", "spoken", via="voice", ts=3.0)
    assert text.via == "text" and voice.via == "voice"
    assert [m.via for m in store.messages_list("c")] == ["text", "voice"]


async def test_async_twin_carries_via(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        await s.conversation_create("c", "t", ts=1.0)
        row = await s.message_append("c", "assistant", "said", via="voice", ts=2.0)
        assert row.via == "voice"
        assert (await s.messages_list("c"))[0].via == "voice"
    finally:
        await s.aclose()
```

Append to `tests/sentinel/test_settings_registry.py`:
```python
def test_voice_keys():
    from friday.sentinel.settings_registry import GROUP_ORDER
    assert GROUP_ORDER == ("llm", "desktop", "sentinel", "voice", "controls")
    assert spec_for("voice.enabled").type == "bool" and spec_for("voice.enabled").default is True
    assert spec_for("voice.name").default == "Aoede"
    assert spec_for("voice.name").choices == ("Aoede", "Kore", "Charon", "Fenrir", "Puck")
    assert validate(spec_for("voice.name"), "Kore") == "Kore"
    with pytest.raises(SettingValidationError):
        validate(spec_for("voice.name"), "Siri")
    assert [g["name"] for g in schema()] == ["llm", "desktop", "sentinel", "voice", "controls"]
```
and in the same file change the two existing assertions that spell out the group list
(`test_schema_has_groups_and_no_values` and `test_sentinel_and_monitor_keys`) to the new
five-group order.

In `tests/sentinel/test_chat_api.py::test_message_stream_and_persistence`, after the
`[m["role"] for m in body["messages"]]` assertion add:
```python
    assert {m["via"] for m in body["messages"]} == {"text"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/core/test_storage_v4.py tests/sentinel/test_settings_registry.py -q`
Expected: `schema_version() == 3`, `TypeError: message_append() got an unexpected keyword argument 'via'`, and the missing voice keys.

- [ ] **Step 3: Implement the migration**

In `friday/core/storage.py` add migration 4 after the `3:` entry:
```python
    4: (
        "ALTER TABLE messages ADD COLUMN via TEXT NOT NULL DEFAULT 'text'",
    ),
```
Add `via: str` as the **last** field of `MessageRow`, extend `_MESSAGE_COLUMNS` with `, via`, and
read it in `_row_to_message`:
```python
_MESSAGE_COLUMNS = ("id, conversation_id, seq, role, content, tool_name, tool_args, tool_result, "
                    "status, ts, via")
```
```python
def _row_to_message(row: sqlite3.Row) -> MessageRow:
    return MessageRow(row["id"], row["conversation_id"], row["seq"], row["role"], row["content"],
                      row["tool_name"], json.loads(row["tool_args"]) if row["tool_args"] else None,
                      row["tool_result"], row["status"], row["ts"], row["via"])
```
In `Store.message_append` add the keyword, the column and the returned field:
```python
    def message_append(self, conversation_id: str, role: str, content: str, *,
                       tool_name: str | None = None, tool_args: dict | None = None,
                       tool_result: str | None = None, status: str = "complete",
                       via: str = "text", ts: float) -> MessageRow:
```
```python
            row_id = c.execute(
                "INSERT INTO messages (conversation_id, seq, role, content, tool_name, tool_args, "
                "tool_result, status, ts, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, seq, role, content, tool_name, args_text, tool_result, status,
                 ts, via)).lastrowid
```
```python
        return MessageRow(int(row_id), conversation_id, seq, role, content, tool_name, tool_args,
                          tool_result, status, ts, via)
```
and the async twin:
```python
    async def message_append(self, conversation_id: str, role: str, content: str, *,
                             tool_name: str | None = None, tool_args: dict | None = None,
                             tool_result: str | None = None, status: str = "complete",
                             via: str = "text", ts: float) -> MessageRow:
        return await self.run(self._store.message_append, conversation_id, role, content,
                              tool_name=tool_name, tool_args=tool_args, tool_result=tool_result,
                              status=status, via=via, ts=ts)
```

In `friday/sentinel/web.py`, `_message_dict` gains `"via": row.via`.

Bump the three existing `assert s.schema_version() == 3` in `tests/core/test_storage.py` and the
one in each of `tests/core/test_storage_v2.py` and `tests/core/test_storage_v3.py` to `== 4`.

- [ ] **Step 4: Implement the registry keys**

In `friday/sentinel/settings_registry.py` set
`GROUP_ORDER = ("llm", "desktop", "sentinel", "voice", "controls")` and append to `REGISTRY`
(before the `controls.*` block, so the file reads in group order):
```python
    SettingSpec("voice.enabled", "bool", "voice",
                "Allow the dashboard to open a voice session with FRIDAY", default=True),
    SettingSpec("voice.name", "enum", "voice",
                "The sentinel's Gemini Live voice (the desktop has its own)",
                default="Aoede", choices=("Aoede", "Kore", "Charon", "Fenrir", "Puck")),
```

In `.env.template`, nothing changes: both keys are vault-only with no legacy variable.

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/core tests/sentinel -q`
Expected: all PASS.

---

### Task 7: `Services.live_client()` and `friday.sentinel.voice`

**Files:**
- Create: `friday/sentinel/voice.py`
- Modify: `friday/sentinel/services.py`
- Test: `tests/sentinel/test_voice.py` (first half)

**Interfaces:**
- Consumes: `LiveSession`, `LiveConfig`, events (Tasks 1–3); `ToolSet`, `SYSTEM_PROMPT` from
  `friday.sentinel.assistant`; storage v4 (Task 6).
- Produces on `Services`: `live_client() -> Any` (a google-genai client built from the vault's
  key, raising `ConfigError` when unset) and `live_model() -> str` (the model from
  `llm.routes.live`, raising `ConfigError` for a non-`gemini` provider).
- Produces in `friday/sentinel/voice.py`: `class WebSocketTransport(ws)` implementing
  `AudioTransport` with `feed(pcm)` (called by the route for each inbound binary frame) and
  `feed_eof()`; `class VoiceSession(services, user, conversation_id, ws)` with
  `async run()`, `feed_audio(pcm)`, `feed_text(text)`, `stop()`, and `conversation_id`.

- [ ] **Step 1: Write the failing tests**

`tests/sentinel/test_voice.py`:
```python
import asyncio
import json

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
    seen = []
    monkeypatch.setattr(services_module, "gemini_client", lambda settings: seen.append(settings) or "CLIENT")
    with pytest.raises(ConfigError):
        services.live_client()                       # nothing in env or vault

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
        ScriptedLive.instances.append(self)

    def push(self, *events):
        for event in events:
            self._queue.put_nowait(event)

    async def run(self):
        await asyncio.sleep(3600)                    # the test stops it

    def stop(self):
        self.stopped = True
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

    assert ws.binary == [b"speech"]
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
    actions = [r.action for r in await services.store.audit_list()]
    assert actions.count("voice.session") == 1
    entry = [r for r in await services.store.audit_list() if r.action == "voice.session"][0]
    assert entry.actor == "user:vince" and entry.detail["conversation_id"] == "c1"
    assert entry.detail["seconds"] >= 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_voice.py -q`
Expected: `ModuleNotFoundError: No module named 'friday.sentinel.voice'`.

- [ ] **Step 3: Extend `Services`**

In `friday/sentinel/services.py` import the client factory and the route parser:
```python
from friday.core.llm import LLMProvider, gemini_client, get_provider
from friday.core.llm.routing import parse_route
from friday.core.config import ConfigError, Settings, apply_overrides
```
and add two methods next to `provider_for`:
```python
    def _vault_settings(self, role: str) -> Settings:
        overlay = {key: self.config.get(key) for key in ("llm.gemini_api_key", f"llm.routes.{role}")}
        return apply_overrides(self.settings, overlay)

    def live_client(self) -> Any:
        """The google-genai client for Live sessions: vault key first, .env fallback."""
        return gemini_client(self._vault_settings("live"))

    def live_model(self) -> str:
        route = parse_route(self.config.get("llm.routes.live"))
        if route.provider != "gemini":
            raise ConfigError(f"llm.routes.live: only the gemini provider supports Live audio, "
                              f"got {route.provider!r}")
        return route.model
```
and simplify `provider_for` to reuse the helper:
```python
    def provider_for(self, role: str) -> LLMProvider:
        """An LLM provider for ``role`` with the vault's key and route laid over .env."""
        return get_provider(self._vault_settings(role), role)
```

- [ ] **Step 4: Implement `friday/sentinel/voice.py`**

```python
"""The sentinel's own voice: a Live session bridged to a dashboard WebSocket.

The browser owns the microphone and the speakers; this module owns the session,
the tools (the same seven the text assistant has) and the transcript that lands
in the conversation the user has open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from friday.core.live import (AudioOut, Connected, Disconnected, Interrupted, LiveConfig,
                              LiveSession, TextOut, ToolFinished, ToolStarted, Transcript,
                              TurnComplete)
from friday.sentinel.assistant import DEFAULT_TITLE, SYSTEM_PROMPT, ToolSet, title_from
from friday.sentinel.auth import User

log = logging.getLogger(__name__)


class WebSocketTransport:
    """AudioTransport over an aiohttp WebSocketResponse: binary frames both ways,
    one JSON frame to tell the browser to drop buffered playback."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.started = False

    def feed(self, pcm: bytes) -> None:
        self._inbound.put_nowait(pcm)

    def feed_eof(self) -> None:
        self._inbound.put_nowait(None)

    async def start(self) -> None:
        self.started = True

    async def read(self) -> bytes | None:
        return await self._inbound.get()

    async def write(self, pcm: bytes) -> None:
        try:
            await self._ws.send_bytes(pcm)
        except Exception as e:                      # the browser went away
            log.debug("voice: could not write audio: %s", e)

    async def clear(self) -> None:
        try:
            await self._ws.send_json({"type": "clear"})
        except Exception:
            pass

    async def stop(self) -> None:
        self.feed_eof()


class VoiceSession:
    """One browser voice session: Live engine + transport + transcript."""

    def __init__(self, services: Any, user: User, conversation_id: str, ws: Any):
        self._svc = services
        self._user = user
        self.conversation_id = conversation_id
        self._ws = ws
        self._transport = WebSocketTransport(ws)
        self._toolset = ToolSet(services, user)
        self._pending: dict[str, str] = {"user": "", "assistant": ""}
        self._live: LiveSession | None = None
        self._started_at = 0.0

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        config = LiveConfig(
            model=self._svc.live_model(),
            voice=self._svc.config.get("voice.name"),
            system_instruction=SYSTEM_PROMPT.format(node_id=self._svc.settings.node_id),
            tools=tuple(ToolSet.declarations),
            input_transcription=True,
            output_transcription=True,
        )
        self._live = LiveSession(self._svc.live_client(), config,
                                 tool_handler=self._call_tool, transport=self._transport,
                                 logger=log)
        self._started_at = time.monotonic()
        runner = asyncio.create_task(self._live.run())
        try:
            await self._consume(self._live)
        finally:
            self._live.stop()
            await asyncio.gather(runner, return_exceptions=True)
            await self._svc.audit(f"user:{self._user.username}", "voice.session", None,
                                  {"conversation_id": self.conversation_id,
                                   "seconds": round(time.monotonic() - self._started_at, 1)})

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
        self._transport.feed_eof()

    # ------------------------------------------------------- browser → model

    def feed_audio(self, pcm: bytes) -> None:
        self._transport.feed(pcm)

    async def feed_text(self, text: str) -> None:
        if self._live is not None:
            await self._live.send_text(text)

    # ------------------------------------------------------- model → browser

    async def _consume(self, live: LiveSession) -> None:
        async for event in live.events():
            try:
                if isinstance(event, Transcript):
                    await self._on_transcript(event)
                elif isinstance(event, TurnComplete):
                    await self._flush("user")
                    await self._flush("assistant")
                    await self._send({"type": "turn_complete"})
                elif isinstance(event, TextOut):
                    self._pending["assistant"] += event.text
                elif isinstance(event, Interrupted):
                    await self._flush("assistant")
                elif isinstance(event, ToolStarted):
                    await self._send({"type": "tool", "phase": "start", "name": event.name,
                                      "args": event.args})
                elif isinstance(event, ToolFinished):
                    await self._send({"type": "tool", "phase": "done", "name": event.name,
                                      "output": event.output, "ms": event.ms})
                elif isinstance(event, Connected):
                    await self._send({"type": "state", "value": "live"})
                elif isinstance(event, Disconnected):
                    await self._send({"type": "state",
                                      "value": "reconnecting" if event.will_retry else "closed"})
                elif isinstance(event, AudioOut):
                    pass                            # the transport already wrote it
            except Exception as e:
                log.info("voice: event handling failed: %s", e)

    async def _on_transcript(self, event: Transcript) -> None:
        # Gemini streams a transcript as fragments; ``final`` marks the last one.
        # Accumulate, then flush — TurnComplete flushes too, in case the flag
        # never arrives.
        self._pending[event.role] = (self._pending.get(event.role) or "") + event.text
        await self._send({"type": "transcript", "role": event.role,
                          "text": self._pending[event.role], "final": event.final})
        if event.final:
            await self._flush(event.role)

    async def _flush(self, role: str) -> None:
        text = (self._pending.get(role) or "").strip()
        self._pending[role] = ""
        if not text:
            return
        conv = await self._svc.store.conversation_get(self.conversation_id)
        if conv is None:
            return
        await self._svc.store.message_append(self.conversation_id, role, text,
                                             via="voice", ts=time.time())
        if role == "user" and conv.title == DEFAULT_TITLE:
            await self._svc.store.conversation_touch(self.conversation_id, time.time(),
                                                     title=title_from(text))

    async def _send(self, obj: dict) -> None:
        try:
            await self._ws.send_json(obj)
        except Exception as e:
            log.debug("voice: could not send %s: %s", obj.get("type"), e)

    # ------------------------------------------------------------------ tool

    async def _call_tool(self, name: str, args: dict) -> tuple[str, bytes | None]:
        return await self._toolset.call(name, args), None
```

Before writing the file, make the title helper public — `voice.py` needs it and a private name
across modules is a smell. In `friday/sentinel/assistant.py` rename `def _title_from(` to
`def title_from(` and its one call in `run_turn` (`title=_title_from(text)` →
`title=title_from(text)`). Nothing else references it:
`grep -rn "_title_from" friday tests` must come back empty afterwards.

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel/test_voice.py tests/sentinel/test_assistant.py tests/sentinel/test_services.py -q`
Expected: all PASS.

---

### Task 8: `GET /voice/ws`

**Files:**
- Modify: `friday/sentinel/web.py`
- Test: `tests/sentinel/test_voice.py` (append the route tests)

**Interfaces:**
- Consumes: `VoiceSession` (Task 7), `require_principal` and `SERVICES` (sub-project 1).
- Produces: route `GET /voice/ws`. Query `?conversation=<id>` selects the thread; omitted or
  unknown creates one. Refused 503 when `voice.enabled` is false, 401 without a principal,
  409 when that conversation already has a voice session.
- Produces on `Services`: `voice_sessions: dict[str, VoiceSession]` (keyed by conversation id).

- [ ] **Step 1: Write the failing tests**

Append to `tests/sentinel/test_voice.py`:
```python
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


import time


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
    live.push(AudioOut(pcm=b"model-speech"))
    frame = await asyncio.wait_for(ws.receive(), 2)
    assert frame.type is aiohttp.WSMsgType.BINARY and frame.data == b"model-speech"
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
    from friday.core.config import ConfigError

    def no_client():
        raise ConfigError("GEMINI_API_KEY is not set")
    monkeypatch.setattr(services, "live_client", no_client)
    ws = await client.ws_connect("/voice/ws")
    frame = await asyncio.wait_for(ws.receive(), 2)
    assert frame.type is aiohttp.WSMsgType.TEXT
    body = json.loads(frame.data)
    assert body["type"] == "error" and "Settings" in body["message"]
    await ws.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_voice.py -q`
Expected: 404s on `/voice/ws`.

- [ ] **Step 3: Implement the route**

In `friday/sentinel/web.py` add the import and handler (after `chat_message`):
```python
from friday.sentinel.voice import VoiceSession
```
```python
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

    try:
        session = VoiceSession(svc, user, conv_id, ws)
        svc.voice_sessions[conv_id] = session
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return ws

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
    return ws
```
Add to the imports at the top of the module: `from aiohttp import WSMsgType, web`,
`from friday.sentinel.assistant import DEFAULT_TITLE, run_turn` (extend the existing line),
`from friday.sentinel.auth import ... User` (already imported), and
`log = logging.getLogger(__name__)` with `import logging` if not present.

A `ConfigError` from `live_client()` or `live_model()` surfaces inside `session.run()`; wrap the
body of `VoiceSession.run()`'s config build in `friday/sentinel/voice.py` so it reports instead
of crashing the socket:
```python
    async def run(self) -> None:
        try:
            model, client = self._svc.live_model(), self._svc.live_client()
        except ConfigError as e:
            await self._send({"type": "error", "message": f"{e} — set the Gemini key under Settings → llm."})
            return
        config = LiveConfig(
            model=model,
            ...
        self._live = LiveSession(client, config, ...)
```
(import `ConfigError` from `friday.core.config` in `voice.py`).

Register the route in `add_web_routes`, next to `/config`:
```python
        web.get("/voice/ws", voice_ws),
```

In `friday/sentinel/services.py` add the registry beside `chat_locks`:
```python
    voice_sessions: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/sentinel -q && .venv/bin/pytest -q`
Expected: all PASS.

---

### Task 9: Dashboard — orb, mic button, voice client

**Files:**
- Create: `friday/sentinel/dashboard/views/voice.js`
- Modify: `friday/sentinel/dashboard/views/assistant.js`, `friday/sentinel/dashboard/index.html`, `friday/sentinel/dashboard/tailwind.src.css`, `friday/sentinel/dashboard/tailwind.css` (rebuild)
- Test: `tests/sentinel/test_dashboard_files.py` (extend)

**Interfaces:**
- Consumes: `api` (`api.js`), `el`/`cls`/`toast` (`ui.js`), `mountOrb` from `shared/orb.js` (Task 5), `GET /voice/ws` (Task 8).
- Produces in `views/voice.js`: `export function createVoice({ orbStage, onTranscript, onTool, onTurnComplete, conversationId })` returning
  `{ toggle(), stop(), get active(), get supported() }`; it owns `getUserMedia`, the 16 kHz
  capture worklet, the 24 kHz playback queue, the `/voice/ws` socket and the orb's state/level feed.
- Produces in `views/assistant.js`: an orb stage above the thread and a mic button beside Send.

- [ ] **Step 1: Extend the tests**

In `tests/sentinel/test_dashboard_files.py` add `"/static/views/voice.js"` to the served-paths
tuple and append:
```python
def test_assistant_view_wires_voice_and_the_orb():
    src = (DASHBOARD_DIR / "views" / "assistant.js").read_text()
    assert 'import { createVoice } from "./voice.js"' in src
    assert "orbStage" in src
    html = HTML.read_text()
    assert '"three": "./shared/three.module.min.js"' in html      # importmap for the orb


def test_voice_module_uses_relative_urls_and_the_shared_orb():
    src = (DASHBOARD_DIR / "views" / "voice.js").read_text()
    assert 'from "../../../webassets/orb.js"' not in src        # served, not imported by path
    assert '"shared/orb.js"' in src or "shared/orb.js" in src
    assert "voice/ws" in src and not re.search(r'["\']/voice/ws', src)
    assert "getUserMedia" in src and "24000" in src and "16000" in src
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/sentinel/test_dashboard_files.py -q`
Expected: FAIL — `views/voice.js` missing, no importmap.

- [ ] **Step 3: Add the importmap to the dashboard shell**

In `friday/sentinel/dashboard/index.html`, immediately before the existing
`<script type="module" src="static/app.js"></script>`:
```html
  <script type="importmap">
    { "imports": { "three": "./shared/three.module.min.js" } }
  </script>
```

- [ ] **Step 4: Write `views/voice.js`**

```js
/* Browser half of the sentinel's voice session.
 *
 * Mic: 16 kHz mono PCM16 in binary frames. Speaker: 24 kHz mono PCM16 scheduled
 * through an AudioContext. Control frames are JSON. The orb is fed state and a
 * live mic level; everything else the page needs comes back through callbacks.
 */
import { toast } from "../ui.js";

const MIC_RATE = 16000;
const SPEAKER_RATE = 24000;

export function createVoice({ orbStage, onTranscript, onTool, onTurnComplete, conversationId }) {
  const state = { ws: null, stream: null, ctx: null, playCtx: null, node: null, analyser: null,
                  orb: null, playAt: 0, sources: new Set(), raf: 0, active: false };

  const supported = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && (window.AudioContext || window.webkitAudioContext));

  async function ensureOrb() {
    if (state.orb || !orbStage) return;
    try {
      const mod = await import("shared/orb.js");
      state.orb = mod.mountOrb(orbStage);
    } catch (e) {
      console.warn("orb unavailable", e);
    }
  }

  function setOrbState(name) {
    if (state.orb) state.orb.setState(name);
  }

  function pumpLevel() {
    state.raf = requestAnimationFrame(pumpLevel);
    if (!state.analyser || !state.orb) return;
    const data = new Uint8Array(state.analyser.frequencyBinCount);
    state.analyser.getByteFrequencyData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i];
    state.orb.setLevel(Math.min(1, (sum / data.length) / 96));
  }

  function wsUrl() {
    const url = new URL("voice/ws", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    if (conversationId && conversationId()) url.searchParams.set("conversation", conversationId());
    return url.toString();
  }

  function playChunk(buffer) {
    const ctx = state.playCtx;
    if (!ctx) return;
    const pcm = new Int16Array(buffer);
    if (!pcm.length) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const audio = ctx.createBuffer(1, f32.length, SPEAKER_RATE);
    audio.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    state.playAt = Math.max(now + 0.02, state.playAt);
    src.start(state.playAt);
    state.playAt += audio.duration;
    state.sources.add(src);
    src.onended = () => {
      state.sources.delete(src);
      if (!state.sources.size) setOrbState("listening");
    };
    setOrbState("speaking");
  }

  function clearPlayback() {
    state.sources.forEach((src) => { try { src.stop(); } catch (e) { /* already ended */ } });
    state.sources.clear();
    state.playAt = 0;
    setOrbState("listening");
  }

  async function startCapture() {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: MIC_RATE, echoCancellation: true,
               noiseSuppression: true, autoGainControl: true },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.ctx = new Ctx({ sampleRate: MIC_RATE });
    state.playCtx = new Ctx();
    if (state.playCtx.state === "suspended") await state.playCtx.resume();

    const source = state.ctx.createMediaStreamSource(state.stream);
    state.analyser = state.ctx.createAnalyser();
    state.analyser.fftSize = 64;
    source.connect(state.analyser);

    state.node = state.ctx.createScriptProcessor(2048, 1, 1);
    source.connect(state.node);
    state.node.connect(state.ctx.destination);
    state.node.onaudioprocess = (event) => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      state.ws.send(pcm.buffer);
    };
    state.raf = requestAnimationFrame(pumpLevel);
  }

  function openSocket() {
    const ws = new WebSocket(wsUrl());
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) { playChunk(event.data); return; }
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "state") {
        if (msg.value === "reconnecting" || msg.value === "closed") clearPlayback();
        setOrbState(msg.value === "live" ? "listening"
          : msg.value === "reconnecting" ? "thinking" : "offline");
      }
      else if (msg.type === "clear") clearPlayback();
      else if (msg.type === "transcript" && onTranscript) onTranscript(msg);
      else if (msg.type === "tool" && onTool) onTool(msg);
      else if (msg.type === "turn_complete" && onTurnComplete) onTurnComplete();
      else if (msg.type === "error") { toast(msg.message, { error: true }); stop(); }
    };
    ws.onclose = () => { if (state.active) stop(); };
    ws.onerror = () => { toast("Voice connection failed", { error: true }); };
  }

  async function start() {
    if (state.active) return;
    if (!supported) { toast("This browser cannot capture audio", { error: true }); return; }
    await ensureOrb();
    setOrbState("thinking");
    try {
      await startCapture();
    } catch (e) {
      setOrbState("offline");
      toast(`Microphone unavailable: ${e.message}`, { error: true });
      await teardown();
      return;
    }
    openSocket();
    state.active = true;
  }

  async function teardown() {
    cancelAnimationFrame(state.raf);
    clearPlayback();
    if (state.node) { state.node.onaudioprocess = null; state.node.disconnect(); state.node = null; }
    if (state.stream) { state.stream.getTracks().forEach((t) => t.stop()); state.stream = null; }
    for (const key of ["ctx", "playCtx"]) {
      if (state[key]) { try { await state[key].close(); } catch (e) { /* already closed */ } state[key] = null; }
    }
    state.analyser = null;
  }

  function stop() {
    if (!state.active && !state.ws) return;
    state.active = false;
    if (state.ws) { try { state.ws.close(); } catch (e) { /* already closing */ } state.ws = null; }
    teardown();
    setOrbState("idle");
  }

  return {
    async toggle() { if (state.active) stop(); else await start(); },
    stop,
    get active() { return state.active; },
    get supported() { return supported; },
  };
}
```

- [ ] **Step 5: Wire it into the Assistant view**

In `friday/sentinel/dashboard/views/assistant.js`:

1. add the import next to the others:
   ```js
   import { createVoice } from "./voice.js";
   ```
2. inside `mount`, after `const stopButton = …`, add the orb stage, the voice client and the
   mic button:
   ```js
   const orbStage = el("div", { class: "h-40 w-full" });
   const caption = el("div", { class: "min-h-[1.25rem] text-center text-xs text-zinc-400" });
   const voice = createVoice({
     orbStage,
     conversationId: () => state.currentId,
     onTranscript: (msg) => {
       caption.textContent = msg.text;
       if (msg.final) caption.textContent = "";
     },
     onTool: (msg) => {
       if (msg.phase === "done") toast(`${msg.name} · ${msg.ms} ms`);
     },
     onTurnComplete: async () => {
       caption.textContent = "";
       try { await select(state.currentId); } catch (e) { /* signed out */ }
     },
   });
   const micButton = el("button", {
     class: "btn", text: "Voice",
     onclick: async () => {
       await voice.toggle();
       micButton.className = cls("btn", voice.active && "btn-primary");
       micButton.textContent = voice.active ? "Stop voice" : "Voice";
     },
   });
   if (!voice.supported) {
     micButton.disabled = true;
     micButton.title = "This browser cannot capture audio";
   }
   ```
3. put the orb and caption above the thread card and the mic button beside Send — in the
   `root.append(...)` call, the middle column becomes:
   ```js
     el("div", { class: "flex min-w-0 flex-1 flex-col gap-3" }, [
       el("div", { class: "flex gap-2 md:hidden" }, [picker, el("button", { class: "btn", text: "New", onclick: () => newButton.click() })]),
       orbStage,
       caption,
       el("section", { class: "card flex min-h-0 flex-1 flex-col" }, [thread]),
       el("div", { class: "flex items-end gap-2" }, [input, micButton, stopButton, sendButton]),
     ]),
   ```
4. the returned handle stops voice on unmount:
   ```js
   return { unmount: () => { voice.stop(); if (state.controller) state.controller.abort(); },
            refresh: loadList };
   ```
5. render a mic glyph on voice turns: in `renderThread`, the user branch becomes
   ```js
       if (m.role === "user") { thread.append(userBubble(m.content, m.via)); return; }
   ```
   and `userBubble` gains the marker:
   ```js
   function userBubble(content, via) {
     const bubble = el("div", { class: "ml-auto max-w-[85%] whitespace-pre-wrap rounded-xl border border-indigo-500/40 bg-indigo-500/10 px-4 py-3", text: content });
     if (via === "voice") bubble.prepend(el("span", { class: "mr-2 text-xs text-indigo-300", text: "🎙" }));
     return bubble;
   }
   ```
   and the assistant branch passes it too:
   ```js
       thread.append(assistantBubble(m.content, steps, m.status, m.via));
   ```
   with
   ```js
   function assistantBubble(content, steps, status, via) {
     const body = el("div", { class: "prose-friday space-y-2" });
     body.append(render(content));
     return el("div", { class: "mr-auto max-w-[85%] rounded-xl border border-zinc-800 bg-zinc-900 px-4 py-3" }, [
       toolAccordion(steps), body, statusTag(status),
       via === "voice" ? el("span", { class: "ml-2 text-xs text-zinc-500", text: "🎙" }) : null]);
   }
   ```
   (the two existing `assistantBubble(...)` calls in `renderThread` and `liveBubble`'s
   fallthrough pass `undefined` for `via`, which renders nothing.)

- [ ] **Step 6: Rebuild the CSS and run the tests**

Run: `deploy/build_css.sh && .venv/bin/pytest tests/sentinel/test_dashboard_files.py -q && .venv/bin/pytest -q`
Expected: all PASS. A `test_css_covers_every_class` failure lists the missing tokens — they are
classes typed into a literal that Tailwind's content scan did not see; fix the source, rebuild.

- [ ] **Step 7: Manual pass**

With the sentinel running and a Gemini key in the vault, open `/assistant`:
1. the orb renders (idle) and the Voice button is enabled;
2. click Voice → the browser asks for the mic once → the orb goes listening;
3. say "how are the nodes doing?" → captions appear, FRIDAY answers in voice, the orb goes
   speaking, and when the turn ends the transcript is in the thread with a 🎙 marker.
   **Check the transcript text is not duplicated** ("how are the nodes doing? how are the nodes
   doing?"): `VoiceSession._on_transcript` assumes Gemini streams transcript *fragments*. If the
   API turns out to send cumulative text, change that one line from `+= event.text` to
   `= event.text` and adjust `test_final_transcripts_are_persisted_with_via_voice`;
4. interrupt FRIDAY mid-sentence → playback stops immediately;
5. say "mute calls" → Controls shows Mute and Activity shows the audit entry with actor
   `user:vince via assistant`;
6. click Stop voice → the browser's mic indicator goes out;
7. at phone width the layout still works and the orb shrinks with the column.

---

### Task 10: Docs and final verification

**Files:**
- Modify: `readme.md`, `deploy/README.md`

- [ ] **Step 1: `deploy/README.md`**

In the Dashboard section, after the paragraph about the pages, add:
````markdown
The Assistant page also talks. **Voice** opens a Gemini Live session on the sentinel over
`/voice/ws` (binary PCM: 16 kHz up, 24 kHz down) with the same seven tools the text chat has, so
"mute calls" works by voice and lands in the audit trail. The session is explicit — it starts
when you click and ends when you click again, navigate away or close the tab. Turn it off
entirely with `voice.enabled` under **Settings → voice**; pick the server's voice with
`voice.name` (the Mac HUD keeps its own, `desktop.voice`).

The orb and its Three.js runtime are shared between both nodes: they live in
`friday/webassets/` and are served at `/shared` by the sentinel and by the desktop hub.
````
In the API table add:
```
| `GET /voice/ws?conversation=<id>` | node token or session | Live voice: binary PCM both ways, JSON control frames |
```

- [ ] **Step 2: `readme.md`**

- In **What the sentinel does**, add a bullet:
  "**Voice.** The sentinel runs its own Gemini Live session for the dashboard — same tools as the
  text assistant, transcripts persisted into the same thread. The engine is
  `friday.core.live`, shared with the desktop hub and (later) the call agent."
- After the **Text assistant** subsection add:
  ```markdown
  ### The Live engine

  `friday.core.live` owns every Gemini Live session in the project: connect, resume, rotate on
  `GoAway`, decode the stream into typed events (`AudioOut`, `TextOut`, `Transcript`,
  `Interrupted`, `TurnComplete`, `ToolStarted`/`ToolFinished`, `GoAway`), and answer tool calls —
  including the ones that raise, because an unanswered turn never closes and the model re-issues
  it after every resume. Consumers push media with `send_audio` / `send_video` / `send_text` and
  react to `session.events()`. An optional `AudioTransport` lets the session pump audio itself:
  the dashboard passes a WebSocket transport, the desktop hub passes none and keeps its own
  mic/playback multiplexing, and sub-project 6's call agent will pass an ALSA one.
  ```
- In **Repository Structure**, under `core/` add
  `│   │   ├── live/                  # Gemini Live: session, events, audio transports`
  and a new top-level entry after `friday/desktop/`:
  `│   ├── webassets/                 # shared browser assets: orb.js + three.module.min.js (served at /shared)`;
  under `sentinel/` add `│       ├── voice.py               # /voice/ws bridge: Live session ↔ browser audio`;
  under `dashboard/` extend the file list with `views/voice.js`.
- In **Sentinel API**, add the `/voice/ws` row from Step 1.
- In **Tests**, extend the sentence with: "the Live engine against a scripted fake client (turn
  loop, interruption, tool answers including a raising tool, `GoAway` rotation, resume-handle
  rules, backoff), and the voice bridge end to end (auth, frame routing, transcript persistence)".
- In **Tech Stack** → Frontend, append "; the FRIDAY orb (Three.js) is shared by both nodes".

- [ ] **Step 3: Verify the docs against the source**

Run: `grep -c "voice/ws\|friday.core.live\|webassets" readme.md deploy/README.md`
Expected: at least 4 in `readme.md`, 2 in `deploy/README.md`.
Run: `ls friday/core/live/ friday/webassets/ friday/sentinel/voice.py friday/sentinel/dashboard/views/voice.js`
Expected: every documented path exists.

- [ ] **Step 4: Final verification**

```bash
.venv/bin/pytest -q                                  # all green, 0 skipped on the Mac
.venv/bin/pytest tests/test_boundaries.py -q
deploy/build_css.sh && git status --short friday/sentinel/dashboard/tailwind.css   # no drift
.venv/bin/python -c "import ast; ast.parse(open('friday/desktop/hub.py').read())"
git status --short | wc -l
```
Then, once each:
- the desktop manual pass from Task 4 Step 11 (talk, interrupt, tool, rotate, quit);
- the dashboard manual pass from Task 9 Step 7 (orb, voice turn, barge-in, `set_controls`, stop);
- the phone: open the dashboard over Tailscale, sign in, run one voice turn.

Leave everything uncommitted.
