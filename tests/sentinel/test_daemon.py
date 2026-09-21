import asyncio
import logging
import os
import signal
import threading
import time

import aiohttp
import pytest

from friday.core.config import ConfigError, load_settings
from friday.core.events import Event
from friday.core.storage import Store
from friday.sentinel.daemon import Sentinel
from tests.sentinel.test_bridges import FakeBridge


def _settings(tmp_path, **extra):
    env = {
        "FRIDAY_DATA_DIR": str(tmp_path / "data"),
        "FRIDAY_NODE_ID": "sentinel-test",
        "FRIDAY_SENTINEL_BIND": "127.0.0.1:0",
        "FRIDAY_TELEMETRY_INTERVAL": "0.05",
        "FRIDAY_HEARTBEAT_INTERVAL": "0.05",
        "FRIDAY_LOG_LEVEL": "DEBUG",
        **extra,
    }
    return load_settings(env=env, env_file=tmp_path / "absent.env")


async def _start(sentinel):
    task = asyncio.create_task(sentinel.run())
    await asyncio.wait_for(sentinel.ready.wait(), 10)
    return task


async def test_boot_health_and_sigterm_shutdown(tmp_path):
    settings = _settings(tmp_path)
    sentinel = Sentinel(settings)
    task = await _start(sentinel)
    assert sentinel.api_port

    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{sentinel.api_port}/health") as resp:
            assert resp.status == 200
            assert (await resp.json())["node_id"] == "sentinel-test"

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 10) == 0

    db = settings.data_dir / "sentinel.db"
    wal = settings.data_dir / "sentinel.db-wal"
    assert db.exists()
    assert not wal.exists() or wal.stat().st_size == 0
    store = Store.open(db)
    try:
        types = {r.event.type for r in store.list_events(limit=1000)}
        assert {"sentinel.started", "sentinel.stopping", "node.heartbeat", "telemetry.sample"} <= types
        assert store.queue_depths()["processing"] == 0
        assert store.heartbeats()[0].node_id == "sentinel-test"
    finally:
        store.close()


async def test_request_shutdown_is_thread_safe(tmp_path):
    sentinel = Sentinel(_settings(tmp_path))
    task = await _start(sentinel)
    threading.Thread(target=sentinel.request_shutdown, args=("from thread",)).start()
    assert await asyncio.wait_for(task, 10) == 0


async def test_orphaned_events_are_requeued_and_processed(tmp_path):
    settings = _settings(tmp_path)
    db = settings.data_dir / "sentinel.db"
    seed = Store.open(db)
    seed.enqueue(Event(type="x.y", source="s", id="orphan"))
    seed.claim(1, now=time.time())
    assert seed.queue_depths()["processing"] == 1
    seed.close()

    sentinel = Sentinel(settings)
    task = await _start(sentinel)
    reader = Store.open(db)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if reader.list_events(type="x.y")[0].status == "done":
                break
            await asyncio.sleep(0.02)
        assert reader.list_events(type="x.y")[0].status == "done"
    finally:
        reader.close()
    sentinel.request_shutdown("test done")
    assert await asyncio.wait_for(task, 10) == 0


async def test_bridge_lifecycle(tmp_path):
    FakeBridge.instances.clear()
    sentinel = Sentinel(_settings(tmp_path, FRIDAY_BRIDGES="tests.sentinel.test_bridges.FakeBridge"))
    task = await _start(sentinel)
    bridge = FakeBridge.instances[0]
    assert bridge.started_with is not None
    bus, ctx = bridge.started_with
    assert hasattr(bus, "publish") and ctx.settings.node_id == "sentinel-test"
    sentinel.request_shutdown("test done")
    assert await asyncio.wait_for(task, 10) == 0
    assert bridge.stopped is True


async def test_bad_bridge_config_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        await Sentinel(_settings(tmp_path, FRIDAY_BRIDGES="no.such.Bridge")).run()


async def test_supervisor_restarts_crashed_task(tmp_path):
    sentinel = Sentinel(_settings(tmp_path))
    sentinel.RESTART_BACKOFF_BASE_S = 0.001
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")

    await asyncio.wait_for(sentinel._supervise("flaky", flaky, logging.getLogger("test")), 5)
    assert len(calls) == 3
    assert sentinel.state.restarts["flaky"] == 2


def test_main_reports_config_error(monkeypatch, capsys, tmp_path):
    from friday.sentinel.__main__ import main

    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    monkeypatch.setenv("FRIDAY_BRIDGES", "no.such.Bridge")
    assert main() == 1
    assert "FRIDAY_BRIDGES" in capsys.readouterr().err
