from friday.core.storage import AsyncStore
from friday.sentinel.audit import record
from friday.sentinel.bus import EventBus


async def test_record_writes_row_and_publishes_event(tmp_path):
    store = await AsyncStore.open(tmp_path / "t.db")
    try:
        bus = EventBus(store)
        row_id = await record(store, bus, "sentinel-x", "user:vince", "token.create", "abc", {"name": "d"})
        rows = await store.audit_list()
        assert rows[0].id == row_id and rows[0].actor == "user:vince" and rows[0].detail == {"name": "d"}
        events = await store.list_events(type="audit.entry")
        assert len(events) == 1 and events[0].event.source == "sentinel-x"
        payload = events[0].event.payload
        assert payload["id"] == row_id and payload["action"] == "token.create"
        assert payload["target"] == "abc" and payload["detail"] == {"name": "d"} and payload["ts"] == rows[0].ts
    finally:
        await store.aclose()


async def test_record_without_bus_only_writes(tmp_path):
    store = await AsyncStore.open(tmp_path / "t.db")
    try:
        await record(store, None, "n", "cli", "user.set_password", "vince", {})
        assert len(await store.audit_list()) == 1 and await store.list_events() == []
    finally:
        await store.aclose()
