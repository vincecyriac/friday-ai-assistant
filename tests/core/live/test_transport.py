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
