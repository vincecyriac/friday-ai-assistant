import time

import pytest

from friday.core.config import ConfigError
from friday.core.events import Event, EventValidationError
from friday.core.storage import Store, StoredEvent


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_open_applies_pragmas(tmp_path):
    s = Store.open(tmp_path / "a.db")
    try:
        assert s.pragma("journal_mode") == "wal"
        assert s.pragma("synchronous") == 2          # FULL
        assert s.pragma("foreign_keys") == 1
        assert s.pragma("busy_timeout") == 5000
        assert s.pragma("journal_size_limit") == 67108864
    finally:
        s.close()


def test_open_normal_sync(tmp_path):
    s = Store.open(tmp_path / "a.db", synchronous="normal")
    try:
        assert s.pragma("synchronous") == 1          # NORMAL
    finally:
        s.close()


def test_open_rejects_unknown_sync(tmp_path):
    with pytest.raises(ConfigError):
        Store.open(tmp_path / "a.db", synchronous="OFF")


def test_open_creates_parent_and_schema(tmp_path):
    s = Store.open(tmp_path / "deep" / "er" / "t.db")
    try:
        assert s.schema_version() == 3
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schema_version", "kv", "heartbeats", "events", "telemetry"} <= names
    finally:
        s.close()


def test_reopen_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    Store.open(p).close()
    s = Store.open(p)
    try:
        assert s.schema_version() == 3
    finally:
        s.close()


def test_kv_roundtrip(store):
    assert store.kv_get("missing") is None
    store.kv_set("k", {"a": [1, 2]})
    assert store.kv_get("k") == {"a": [1, 2]}
    store.kv_set("k", 5)
    assert store.kv_get("k") == 5
    store.kv_delete("k")
    assert store.kv_get("k") is None


def test_heartbeat_upsert(store):
    store.heartbeat_upsert("mac", "listening", {"v": "1"}, 100.0)
    store.heartbeat_upsert("mac", "idle", {"v": "2"}, 200.0)
    store.heartbeat_upsert("pi", "running", {}, 150.0)
    rows = {r.node_id: r for r in store.heartbeats()}
    assert set(rows) == {"mac", "pi"}
    assert rows["mac"].status == "idle"
    assert rows["mac"].last_seen == 200.0
    assert rows["mac"].meta == {"v": "2"}


def test_enqueue_claim_complete(store):
    e = Event(type="a.b", source="s", payload={"n": 1})
    store.enqueue(e)
    assert store.queue_depths() == {"pending": 1, "processing": 0, "done": 0, "failed": 0}

    claimed = store.claim(10, now=1000.0)
    assert len(claimed) == 1
    got = claimed[0]
    assert isinstance(got, StoredEvent)
    assert got.event == e
    assert got.status == "processing" and got.attempts == 1 and got.claimed_at == 1000.0
    assert store.claim(10, now=1001.0) == []

    store.complete(e.id, now=1002.0)
    assert store.queue_depths()["done"] == 1
    assert store.list_events(status="done")[0].processed_at == 1002.0


def test_enqueue_validates(store):
    with pytest.raises(EventValidationError):
        store.enqueue(Event(type="bad", source="s"))


def test_claim_orders_by_priority_then_time(store):
    store.enqueue(Event(type="a.b", source="s", ts=3.0, priority=0, id="low-late"))
    store.enqueue(Event(type="a.b", source="s", ts=1.0, priority=0, id="low-early"))
    store.enqueue(Event(type="a.b", source="s", ts=2.0, priority=5, id="high"))
    assert [c.event.id for c in store.claim(10, now=10.0)] == ["high", "low-early", "low-late"]


def test_claim_respects_limit(store):
    for i in range(5):
        store.enqueue(Event(type="a.b", source="s", ts=float(i)))
    assert len(store.claim(2, now=10.0)) == 2
    assert store.queue_depths()["pending"] == 3


def test_fail_retry_then_terminal(store):
    e = Event(type="a.b", source="s")
    store.enqueue(e)
    store.claim(1, now=1.0)
    store.fail(e.id, "boom", now=2.0, retry=True)
    assert store.queue_depths()["pending"] == 1

    again = store.claim(1, now=3.0)
    assert again[0].attempts == 2
    assert again[0].error == "boom"

    store.fail(e.id, "boom again", now=4.0, retry=False)
    assert store.queue_depths()["failed"] == 1
    failed = store.list_events(status="failed")[0]
    assert failed.error == "boom again" and failed.processed_at == 4.0


def test_requeue_stale(store):
    e = Event(type="a.b", source="s")
    store.enqueue(e)
    store.claim(1, now=100.0)
    assert store.requeue_stale(older_than_s=60.0, now=120.0) == 0   # claimed 20 s ago
    assert store.requeue_stale(older_than_s=0.0, now=120.0) == 1
    assert store.queue_depths()["pending"] == 1


def test_prune_keeps_live_queue(store):
    done = Event(type="a.b", source="s")
    store.enqueue(done)
    store.claim(1, now=1.0)
    store.complete(done.id, now=1.0)
    store.enqueue(Event(type="a.b", source="s"))              # stays pending
    store.telemetry_insert("n", {"cpu": 1}, ts=1.0)
    store.telemetry_insert("n", {"cpu": 2}, ts=time.time() + 100)

    counts = store.prune(older_than_ts=time.time() + 10)
    assert counts == {"telemetry": 1, "events": 1}
    assert store.queue_depths() == {"pending": 1, "processing": 0, "done": 0, "failed": 0}
    assert store.telemetry_latest("n") == {"cpu": 2}


def test_telemetry_latest(store):
    assert store.telemetry_latest() is None
    store.telemetry_insert("a", {"v": 1}, ts=1.0)
    store.telemetry_insert("b", {"v": 2}, ts=2.0)
    store.telemetry_insert("a", {"v": 3}, ts=3.0)
    assert store.telemetry_latest() == {"v": 3}
    assert store.telemetry_latest("b") == {"v": 2}
    assert store.telemetry_latest("zzz") is None
    assert store.telemetry_count() == 3
    assert store.telemetry_count("a") == 2


def test_list_events_filters(store):
    store.enqueue(Event(type="x.y", source="s"))
    store.enqueue(Event(type="x.z", source="s"))
    assert [r.event.type for r in store.list_events(type="x.z")] == ["x.z"]
    assert len(store.list_events()) == 2
    assert len(store.list_events(limit=1)) == 1


def test_corrupt_file_is_quarantined(tmp_path):
    p = tmp_path / "t.db"
    p.write_bytes(b"this is not a database " * 100)
    s = Store.open(p)
    try:
        assert s.schema_version() == 3
        s.kv_set("k", 1)
        assert s.kv_get("k") == 1
    finally:
        s.close()
    quarantined = [q for q in tmp_path.glob("t.db.corrupt-*") if not q.name.endswith(("-wal", "-shm"))]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes().startswith(b"this is not")


def test_checkpoint_truncates_wal(tmp_path):
    p = tmp_path / "t.db"
    s = Store.open(p)
    try:
        for i in range(50):
            s.kv_set(f"k{i}", i)
        wal = tmp_path / "t.db-wal"
        assert wal.exists() and wal.stat().st_size > 0
        s.checkpoint("TRUNCATE")
        assert wal.stat().st_size == 0
    finally:
        s.close()
