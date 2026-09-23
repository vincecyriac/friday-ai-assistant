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
