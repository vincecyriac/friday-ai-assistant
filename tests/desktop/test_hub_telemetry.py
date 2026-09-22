"""The desktop's heartbeat loop also ships a telemetry sample. macOS only (imports hub)."""
import asyncio
import dataclasses

import pytest

pytest.importorskip("Quartz", reason="desktop extra not installed")


class FakeClient:
    posted = []

    def __init__(self, url, token, node_id, **kw):
        self.url, self.token, self.node_id = url, token, node_id

    async def post(self, event):
        FakeClient.posted.append(event)
        return True

    async def aclose(self):
        pass


async def test_heartbeat_task_posts_heartbeat_then_telemetry(monkeypatch):
    from friday.desktop import hub

    FakeClient.posted = []
    monkeypatch.setattr(hub, "SentinelClient", FakeClient)
    monkeypatch.setattr(hub, "shutdown_event", asyncio.Event())
    monkeypatch.setattr(hub, "settings", dataclasses.replace(
        hub.settings, sentinel_url="http://127.0.0.1:1", sentinel_token="fn_x", node_id="mac-test",
        heartbeat_interval_s=0.05))

    task = asyncio.create_task(hub.sentinel_heartbeat_task())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(FakeClient.posted) >= 4:
            break
    hub.shutdown_event.set()
    await asyncio.wait_for(task, 5)

    types = [e.type for e in FakeClient.posted[:4]]
    assert types == ["node.heartbeat", "telemetry.sample", "node.heartbeat", "telemetry.sample"]
    sample = FakeClient.posted[1]
    assert sample.source == "mac-test" and sample.payload["node_id"] == "mac-test"
    assert "cpu_percent" in sample.payload and "platform" in sample.payload


async def test_collect_failure_is_skipped(monkeypatch):
    from friday.desktop import hub

    FakeClient.posted = []
    monkeypatch.setattr(hub, "SentinelClient", FakeClient)
    monkeypatch.setattr(hub, "shutdown_event", asyncio.Event())
    monkeypatch.setattr(hub, "settings", dataclasses.replace(
        hub.settings, sentinel_url="http://127.0.0.1:1", sentinel_token=None, node_id="mac-test",
        heartbeat_interval_s=0.05))

    def broken(*args, **kwargs):
        raise RuntimeError("no sensors")
    monkeypatch.setattr(hub, "collect", broken)

    task = asyncio.create_task(hub.sentinel_heartbeat_task())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(FakeClient.posted) >= 2:
            break
    hub.shutdown_event.set()
    await asyncio.wait_for(task, 5)
    assert {e.type for e in FakeClient.posted} == {"node.heartbeat"}
