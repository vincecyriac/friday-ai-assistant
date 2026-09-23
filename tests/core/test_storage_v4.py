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
    old.connection.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, status, ts) "
        "VALUES ('c1', 1, 'user', 'hello', 'complete', 2.0)")
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
