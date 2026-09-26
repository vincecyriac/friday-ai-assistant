import pytest

from friday.core import storage
from friday.core.events import Event
from friday.core.storage import AsyncStore, Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v2_database_upgrades_to_v3(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {k: v for k, v in storage.MIGRATIONS.items() if k <= 2})
    old = Store.open(path)
    old.setting_set("controls.dnd", "true", secret=False, updated_by="t", ts=1.0)
    old.user_upsert("vince", "h", ts=1.0)
    old.node_token_create("i", "n", "hash", ts=1.0)
    assert old.schema_version() == 2
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 5
        assert s.setting_get("controls.dnd").value == "true"
        assert s.user_get("vince") is not None and s.node_token_by_hash("hash") is not None
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"conversations", "messages"} <= names
    finally:
        s.close()


def test_conversation_crud_and_seq(store):
    c = store.conversation_create("c1", "New conversation", ts=1.0)
    assert c.id == "c1" and c.message_count == 0 and c.updated_at == 1.0
    m1 = store.message_append("c1", "user", "hello", ts=2.0)
    m2 = store.message_append("c1", "assistant", "hi", ts=3.0)
    t = store.message_append("c1", "tool", "", tool_name="get_nodes", tool_args={"a": 1}, tool_result="[]", ts=4.0)
    assert (m1.seq, m2.seq, t.seq) == (1, 2, 3) and m1.id < m2.id < t.id
    assert t.tool_args == {"a": 1} and t.tool_result == "[]" and t.status == "complete"
    assert store.conversation_get("c1").updated_at == 4.0 and store.conversation_get("c1").message_count == 3
    store.conversation_touch("c1", ts=5.0, title="hello")
    assert store.conversation_get("c1").title == "hello"
    store.message_set_status(m2.id, "interrupted")
    rows = store.messages_list("c1")
    assert [r.role for r in rows] == ["user", "assistant", "tool"] and rows[1].status == "interrupted"
    assert [r.seq for r in store.messages_list("c1", limit=2)] == [2, 3]
    assert store.conversation_delete("c1") is True
    assert store.conversation_delete("c1") is False
    assert store.messages_list("c1") == [] and store.conversation_get("c1") is None


def test_conversations_list_and_prune(store):
    for i, ts in enumerate((10.0, 30.0, 20.0)):
        store.conversation_create(f"c{i}", f"t{i}", ts=ts)
    assert [c.id for c in store.conversations_list()] == ["c1", "c2", "c0"]
    assert [c.id for c in store.conversations_list(limit=1)] == ["c1"]
    store.message_append("c0", "user", "x", ts=11.0)
    assert store.conversations_prune(idle_before_ts=25.0) == 2          # c0 (11) and c2 (20)
    assert [c.id for c in store.conversations_list()] == ["c1"]
    assert store.messages_list("c0") == []


def test_list_events_filters(store):
    for i, (t, src, ts) in enumerate([("node.heartbeat", "mac", 1.0), ("telemetry.sample", "mac", 2.0),
                                      ("node.heartbeat", "pi", 3.0), ("audit.entry", "s", 4.0),
                                      ("a_b.cd", "s", 5.0), ("axb.cd", "s", 6.0)]):
        store.enqueue(Event(type=t, source=src, id=f"e{i}", ts=ts))
    ids = lambda rows: [r.event.id for r in rows]
    assert ids(store.list_events(type_glob="node.*")) == ["e2", "e0"]
    assert ids(store.list_events(type_glob="*", source="mac")) == ["e1", "e0"]
    assert ids(store.list_events(since_ts=2.0)) == ["e5", "e4", "e3", "e2", "e1"]
    assert ids(store.list_events(before_ts=3.0)) == ["e1", "e0"]
    assert ids(store.list_events(type_glob="a_b.cd")) == ["e4"]            # _ is literal, not LIKE's wildcard
    assert ids(store.list_events(type_glob="a?b.cd")) == ["e5", "e4"]      # ? is the single-char wildcard
    assert ids(store.list_events(type_glob="node.?eartbeat", limit=1)) == ["e2"]
    assert ids(store.list_events(type="audit.entry")) == ["e3"]           # old exact filter still works


def test_telemetry_latest_all(store):
    assert store.telemetry_latest_all() == {}
    store.telemetry_insert("mac", {"cpu_percent": 1}, ts=1.0)
    store.telemetry_insert("mac", {"cpu_percent": 2}, ts=2.0)
    store.telemetry_insert("pi", {"cpu_percent": 9}, ts=1.5)
    assert store.telemetry_latest_all() == {"mac": {"cpu_percent": 2}, "pi": {"cpu_percent": 9}}


async def test_async_twins(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        await s.conversation_create("c", "t", ts=1.0)
        m = await s.message_append("c", "user", "x", ts=2.0)
        assert (await s.conversation_get("c")).message_count == 1
        assert len(await s.conversations_list()) == 1
        await s.conversation_touch("c", ts=3.0)
        await s.message_set_status(m.id, "error")
        assert (await s.messages_list("c"))[0].status == "error"
        assert await s.conversations_prune(idle_before_ts=0.0) == 0
        assert await s.telemetry_latest_all() == {}
        assert await s.list_events(type_glob="*") == []
        assert await s.conversation_delete("c") is True
    finally:
        await s.aclose()
