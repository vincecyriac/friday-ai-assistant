import json

import pytest

from friday.core.events import Event, EventValidationError, Heartbeat, PowerInfo, TelemetrySnapshot
from friday.core.platform import detect


def test_event_defaults():
    e = Event(type="node.heartbeat", source="mac")
    assert len(e.id) == 32
    assert e.ts > 0
    assert e.priority == 0
    assert e.payload == {}


def test_roundtrip():
    e = Event(type="a.b", source="s", payload={"x": 1, "y": [1, 2]}, priority=2)
    assert Event.from_dict(e.to_dict()) == e
    assert Event.from_dict(json.loads(json.dumps(e.to_dict()))) == e


def test_from_dict_fills_defaults_and_ignores_extras():
    e = Event.from_dict({"type": "a.b", "source": "s", "unknown": 1})
    assert e.id and e.ts > 0 and e.priority == 0 and e.payload == {}


def test_from_dict_null_payload_is_empty():
    assert Event.from_dict({"type": "a.b", "source": "s", "payload": None}).payload == {}


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        {"type": "nodots", "source": "s"},
        {"type": "Upper.Case", "source": "s"},
        {"type": "a.b", "source": ""},
        {"type": "a.b"},
        {"source": "s"},
        {"type": "a.b", "source": "s", "payload": "notadict"},
        {"type": "a.b", "source": "s", "ts": "soon"},
        {"type": "a.b", "source": "s", "priority": "high"},
        {"type": "a.b", "source": "s", "id": ""},
    ],
)
def test_invalid_events(bad):
    with pytest.raises(EventValidationError):
        Event.from_dict(bad)


def test_non_json_payload_rejected():
    with pytest.raises(EventValidationError):
        Event(type="a.b", source="s", payload={"o": object()}).validate()


def test_heartbeat_roundtrip():
    hb = Heartbeat(status="listening", version="0.1.0", platform="Darwin/arm64", meta={"k": 1})
    assert Heartbeat.from_dict(hb.to_dict()) == hb


def test_heartbeat_requires_status():
    with pytest.raises(EventValidationError):
        Heartbeat.from_dict({"version": "1", "platform": "x"})
    with pytest.raises(EventValidationError):
        Heartbeat.from_dict("nope")


def test_snapshot_to_dict_is_json_safe():
    snap = TelemetrySnapshot(
        ts=1.0, node_id="n", platform=detect(), cpu_percent=None, load_avg=(1.0, 2.0, 3.0),
        mem_total=None, mem_used=None, mem_available=None, disk_total=None, disk_used=None,
        disk_free=None, disk_path="/", uptime_s=None, thermal={"cpu": 41.5},
        power=PowerInfo(None, None, None, None),
    )
    d = snap.to_dict()
    json.dumps(d)
    assert d["platform"]["system"]
    assert d["thermal"] == {"cpu": 41.5}
    assert d["power"]["under_voltage"] is None
