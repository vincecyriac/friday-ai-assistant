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
