import pytest

from friday.core import storage
from friday.core.storage import AsyncStore, Store
from friday.sentinel.sources import WatchItem


def item(source="email", external_id="gmail:abc", **kw):
    base = dict(title="Subject", snippet="body", who="Ada", url="https://x/1", ts=100.0, meta={"a": 1})
    return WatchItem(source=source, external_id=external_id, **{**base, **kw})


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "t.db")
    yield s
    s.close()


def test_v4_database_upgrades_to_v5(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "MIGRATIONS", {k: v for k, v in storage.MIGRATIONS.items() if k <= 4})
    old = Store.open(path)
    old.conversation_create("c1", "t", ts=1.0)
    old.kv_set("keep", {"me": True})
    assert old.schema_version() == 4
    old.close()
    monkeypatch.undo()

    s = Store.open(path)
    try:
        assert s.schema_version() == 5
        assert s.conversation_get("c1") is not None and s.kv_get("keep") == {"me": True}
        names = {r[0] for r in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "watch_items" in names
    finally:
        s.close()


def test_add_returns_false_for_a_repeat_and_that_is_the_dedupe(store):
    assert store.watch_item_add(item(), first_seen=1.0) is True
    assert store.watch_item_add(item(), first_seen=2.0) is False
    rows = store.watch_items_list()
    assert len(rows) == 1 and rows[0].first_seen == 1.0        # the first sighting wins


def test_two_mail_accounts_with_the_same_raw_id_do_not_collide(store):
    """A Gmail id and an IMAP UID can both be '12345'. Both must land."""
    assert store.watch_item_add(item(external_id="gmail:12345"), first_seen=1.0) is True
    assert store.watch_item_add(item(external_id="imap:12345"), first_seen=1.0) is True
    assert {r.id for r in store.watch_items_list()} == {"email:gmail:12345", "email:imap:12345"}


def test_rows_round_trip_including_meta(store):
    store.watch_item_add(item(meta={"thread_id": "t1", "unread": 3}), first_seen=5.0)
    row = store.watch_item_get("email:gmail:abc")
    assert row.source == "email" and row.external_id == "gmail:abc" and row.who == "Ada"
    assert row.ts == 100.0 and row.first_seen == 5.0 and row.meta == {"thread_id": "t1", "unread": 3}


def test_list_filters_by_source_and_orders_newest_first(store):
    store.watch_item_add(item(external_id="gmail:1", ts=10.0), first_seen=10.0)
    store.watch_item_add(item(source="jira", external_id="OPS-1", ts=30.0), first_seen=30.0)
    store.watch_item_add(item(external_id="gmail:2", ts=20.0), first_seen=20.0)
    assert [r.id for r in store.watch_items_list()] == ["jira:OPS-1", "email:gmail:2", "email:gmail:1"]
    assert [r.id for r in store.watch_items_list(source="email")] == ["email:gmail:2", "email:gmail:1"]
    assert [r.id for r in store.watch_items_list(limit=1)] == ["jira:OPS-1"]


def test_counts_and_prune(store):
    store.watch_item_add(item(external_id="gmail:old"), first_seen=10.0)
    store.watch_item_add(item(external_id="gmail:new"), first_seen=100.0)
    store.watch_item_add(item(source="jira", external_id="OPS-2"), first_seen=100.0)
    assert store.watch_counts(since_ts=50.0) == {"email": 1, "jira": 1}
    assert store.watch_counts(since_ts=0.0) == {"email": 2, "jira": 1}
    assert store.watch_items_prune(before_ts=50.0) == 1
    assert {r.id for r in store.watch_items_list()} == {"email:gmail:new", "jira:OPS-2"}


def test_a_giant_snippet_is_stored_as_given_but_the_capability_already_capped_it(store):
    """Storage does not truncate; the capability did. This pins the contract so a
    future caller that skips clean_text is visible in review, not in the database."""
    store.watch_item_add(item(snippet="x" * 500), first_seen=1.0)
    assert len(store.watch_item_get("email:gmail:abc").snippet) == 500


async def test_async_twins(tmp_path):
    s = await AsyncStore.open(tmp_path / "a.db")
    try:
        assert await s.watch_item_add(item(), first_seen=1.0) is True
        assert await s.watch_item_add(item(), first_seen=1.0) is False
        assert (await s.watch_item_get("email:gmail:abc")).who == "Ada"
        assert len(await s.watch_items_list()) == 1
        assert await s.watch_counts(since_ts=0.0) == {"email": 1}
        assert await s.watch_items_prune(before_ts=10.0) == 1
    finally:
        await s.aclose()
