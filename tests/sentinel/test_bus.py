import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from friday.core.events import Event, EventValidationError
from friday.core.storage import AsyncStore
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext


class Recorder:
    def __init__(self, name="rec", patterns=("*",), fail_times=0, delay=0.0):
        self.name = name
        self.patterns = patterns
        self.seen = []
        self._fail = fail_times
        self.delay = delay

    async def handle(self, event, ctx):
        self.seen.append(event)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("nope")


async def until(predicate, timeout=3.0):
    """Poll an async predicate until true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def rig(make_settings, tmp_path):
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store, max_attempts=3, handler_timeout_s=0.2, poll_interval_s=0.05)
    ctx = HandlerContext(settings=make_settings(), store=store, bus=bus,
                         logger=logging.getLogger("test.bus"))
    task = asyncio.create_task(bus.run_dispatcher(ctx))
    yield SimpleNamespace(bus=bus, store=store, ctx=ctx, task=task)
    await bus.drain()
    await asyncio.wait_for(task, 2)
    await store.aclose()


async def depth(rig, status):
    return (await rig.store.queue_depths())[status]


def depth_is(rig, status, n):
    """Async predicate for until(): queue depth of ``status`` equals ``n``."""
    async def check():
        return await depth(rig, status) == n
    return check


def seen_count(handler, n):
    async def check():
        return len(handler.seen) == n
    return check


async def test_publish_is_durable_then_dispatched(rig):
    h = Recorder()
    rig.bus.subscribe(h)
    event_id = await rig.bus.publish(Event(type="a.b", source="s", payload={"n": 1}))
    await until(depth_is(rig, "done", 1))
    assert [e.id for e in h.seen] == [event_id]


async def test_pattern_matching(rig):
    a, b = Recorder("a", ("node.*",)), Recorder("b", ("telemetry.sample",))
    rig.bus.subscribe(a)
    rig.bus.subscribe(b)
    await rig.bus.publish(Event(type="node.heartbeat", source="s"))
    await rig.bus.publish(Event(type="telemetry.sample", source="s"))
    await until(depth_is(rig, "done", 2))
    assert [e.type for e in a.seen] == ["node.heartbeat"]
    assert [e.type for e in b.seen] == ["telemetry.sample"]


async def test_publish_validates(rig):
    with pytest.raises(EventValidationError):
        await rig.bus.publish(Event(type="bad", source="s"))
    assert await depth(rig, "pending") == 0


async def test_failing_handler_is_retried_then_parked(rig):
    h = Recorder(fail_times=99)
    rig.bus.subscribe(h)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1))
    assert len(h.seen) == 3
    failed = (await rig.store.list_events(status="failed"))[0]
    assert failed.attempts == 3
    assert "rec: RuntimeError: nope" in failed.error


async def test_recovers_on_retry(rig):
    h = Recorder(fail_times=1)
    rig.bus.subscribe(h)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "done", 1))
    assert len(h.seen) == 2


async def test_one_bad_handler_does_not_block_another(rig):
    bad, good = Recorder("bad", fail_times=99), Recorder("good")
    rig.bus.subscribe(bad)
    rig.bus.subscribe(good)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1))
    assert len(good.seen) == 3        # at-least-once: re-run on every attempt


async def test_handler_timeout_is_enforced(rig):
    slow = Recorder(delay=1.0)
    rig.bus.subscribe(slow)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(depth_is(rig, "failed", 1), timeout=5.0)
    failed = (await rig.store.list_events(status="failed"))[0]
    assert "TimeoutError" in failed.error


async def test_drain_waits_for_in_flight_handler(rig):
    slow = Recorder(delay=0.1)                  # under the rig's 0.2 s handler timeout
    rig.bus.subscribe(slow)
    await rig.bus.publish(Event(type="a.b", source="s"))
    await until(seen_count(slow, 1))
    await rig.bus.drain()                       # returns only once the handler finished
    assert await depth(rig, "done") == 1
    await asyncio.wait_for(rig.task, 1)         # dispatcher exited on its own


def test_subscribe_unsubscribe_and_handlers_for(tmp_path):
    bus = EventBus(store=None)
    a = Recorder("a", ("node.*",))
    bus.subscribe(a)
    assert bus.handlers_for("node.heartbeat") == [a]
    assert bus.handlers_for("x.y") == []
    bus.unsubscribe(a)
    assert bus.handlers_for("node.heartbeat") == []
    bus.unsubscribe(a)                          # idempotent
