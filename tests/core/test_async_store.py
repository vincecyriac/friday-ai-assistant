import threading
import time

import pytest

from friday.core.config import ConfigError
from friday.core.events import Event
from friday.core.storage import AsyncStore


async def test_every_call_runs_on_one_dedicated_thread(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        t1 = await s.run(threading.get_ident)
        t2 = await s.run(threading.get_ident)
        assert t1 == t2
        assert t1 != threading.get_ident()
        name = await s.run(lambda: threading.current_thread().name)
        assert name.startswith("friday-store")
    finally:
        await s.aclose()


async def test_roundtrip_through_async_api(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        e = Event(type="a.b", source="s")
        await s.enqueue(e)
        claimed = await s.claim(1, time.time())
        assert claimed[0].event == e
        await s.complete(e.id, time.time())
        assert (await s.queue_depths())["done"] == 1
        await s.kv_set("k", 1)
        assert await s.kv_get("k") == 1
        await s.heartbeat_upsert("n", "up", {}, 1.0)
        assert (await s.heartbeats())[0].node_id == "n"
        await s.telemetry_insert("n", {"x": 1}, 1.0)
        assert await s.telemetry_latest("n") == {"x": 1}
        assert await s.telemetry_count("n") == 1
        assert s.path == tmp_path / "a.db"
    finally:
        await s.aclose()


async def test_aclose_checkpoints_and_closes(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    for i in range(20):
        await s.kv_set(f"k{i}", i)
    await s.aclose()
    wal = tmp_path / "a.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


async def test_open_failure_propagates(tmp_path):
    with pytest.raises(ConfigError):
        await AsyncStore.open(tmp_path / "a.db", synchronous="OFF")
