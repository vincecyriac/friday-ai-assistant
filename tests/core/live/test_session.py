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
    client = FakeLiveClient([[handle_msg("h-1"), goaway_msg("3s")], [text_msg("after")]])
    _, events = await run_until(client, count=5)
    kinds = [type(e).__name__ for e in events]
    assert kinds[:4] == ["Connected", "GoAway", "Disconnected", "Connected"]
    assert events[1] == GoAway(time_left="3s")
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
    # 1. resume refused at the handshake -> handle dropped, next connect is cold
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
    # The stream ends (it does not hang); the last event is the closing Disconnected.
    tail = [e async for e in session.events()]
    assert all(isinstance(e, Disconnected) for e in tail) and not any(e.will_retry for e in tail)


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
