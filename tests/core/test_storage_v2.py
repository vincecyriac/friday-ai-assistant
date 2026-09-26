import pytest

from friday.core import storage
from friday.core.events import Event
from friday.core.storage import AsyncStore, Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v1_database_upgrades_to_v2_with_rows_intact(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {1: storage.MIGRATIONS[1]})
    old = Store.open(path)
    old.kv_set("k", 1)
    old.enqueue(Event(type="a.b", source="s", id="keep"))
    assert old.schema_version() == 1
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 5
        assert s.kv_get("k") == 1
        assert s.list_events()[0].event.id == "keep"
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"settings", "users", "sessions", "node_tokens", "audit"} <= names
        assert s.users_count() == 0
    finally:
        s.close()


def test_settings_crud(store):
    assert store.setting_get("x") is None
    store.setting_set("llm.k", "v1:abc:def", secret=True, updated_by="cli", ts=1.0)
    store.setting_set("controls.dnd", "true", secret=False, updated_by="dashboard:vince", ts=2.0)
    row = store.setting_get("llm.k")
    assert row.value == "v1:abc:def" and row.secret is True and row.updated_by == "cli" and row.updated_at == 1.0
    store.setting_set("llm.k", "v1:new", secret=True, updated_by="cli", ts=3.0)
    assert store.setting_get("llm.k").value == "v1:new"
    assert [r.key for r in store.settings_all()] == ["controls.dnd", "llm.k"]
    assert store.setting_delete("llm.k") is True
    assert store.setting_delete("llm.k") is False
    assert store.setting_get("llm.k") is None


def test_users(store):
    assert store.user_get("vince") is None
    store.user_upsert("vince", "scrypt$1", ts=10.0)
    store.user_upsert("vince", "scrypt$2", ts=20.0)
    u = store.user_get("vince")
    assert u.password_hash == "scrypt$2" and u.created_at == 10.0 and u.password_changed_at == 20.0
    assert store.users_count() == 1


def test_sessions(store):
    store.session_create("h1", "vince", ts=1.0, expires_at=100.0, user_agent="ua", ip="1.2.3.4")
    store.session_create("h2", "vince", ts=1.0, expires_at=5.0, user_agent=None, ip=None)
    s = store.session_get("h1")
    assert s.username == "vince" and s.last_seen == 1.0 and s.user_agent == "ua" and s.ip == "1.2.3.4"
    store.session_touch("h1", ts=50.0)
    assert store.session_get("h1").last_seen == 50.0
    assert store.sessions_prune(now=10.0) == 1            # h2 expired, h1 kept
    assert store.session_get("h2") is None
    assert store.session_delete("h1") is True
    assert store.session_delete("h1") is False


def test_node_tokens(store):
    store.node_token_create("id1", "desktop", "hash1", ts=1.0)
    store.node_token_create("id2", "phone", "hash2", ts=2.0)
    row = store.node_token_by_hash("hash1")
    assert row.id == "id1" and row.name == "desktop" and row.last_used is None and row.revoked_at is None
    assert not hasattr(row, "token_hash")
    store.node_token_touch("id1", ts=5.0)
    assert store.node_token_by_hash("hash1").last_used == 5.0
    assert store.node_token_revoke("id1", ts=6.0) is True
    assert store.node_token_revoke("id1", ts=6.0) is False
    assert store.node_token_by_hash("hash1") is None
    listed = {r.id: r for r in store.node_tokens_list()}
    assert listed["id1"].revoked_at == 6.0 and listed["id2"].revoked_at is None
    assert store.node_token_by_hash("nope") is None


def test_audit(store):
    ids = [store.audit_append(ts=float(i), actor="cli", action="settings.update", target=f"k{i}", detail={"i": i})
           for i in range(5)]
    assert ids == sorted(ids)
    rows = store.audit_list(limit=3)
    assert [r.target for r in rows] == ["k4", "k3", "k2"]
    assert rows[0].detail == {"i": 4}
    older = store.audit_list(limit=10, before_id=rows[-1].id)
    assert [r.target for r in older] == ["k1", "k0"]


async def test_async_twins(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        await s.setting_set("k", "v", secret=False, updated_by="t", ts=1.0)
        assert (await s.setting_get("k")).value == "v"
        assert [r.key for r in await s.settings_all()] == ["k"]
        await s.user_upsert("u", "h", ts=1.0)
        assert (await s.user_get("u")).password_hash == "h"
        assert await s.users_count() == 1
        await s.session_create("sid", "u", ts=1.0, expires_at=2.0, user_agent=None, ip=None)
        assert (await s.session_get("sid")).username == "u"
        await s.session_touch("sid", ts=1.5)
        assert await s.sessions_prune(now=3.0) == 1
        assert await s.session_delete("sid") is False
        await s.node_token_create("n", "name", "hash", ts=1.0)
        assert (await s.node_token_by_hash("hash")).name == "name"
        await s.node_token_touch("n", ts=2.0)
        assert len(await s.node_tokens_list()) == 1
        assert await s.node_token_revoke("n", ts=3.0) is True
        assert await s.audit_append(ts=1.0, actor="a", action="b", target=None, detail={}) == 1
        assert len(await s.audit_list()) == 1
        assert await s.setting_delete("k") is True
    finally:
        await s.aclose()
