"""Sentinel test rig: a fully wired Services with a running dispatcher."""

import asyncio
import logging
import time

import pytest

from friday.core.platform import detect
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext, HeartbeatHandler, TelemetryHandler
from friday.sentinel.runtime_config import RuntimeConfig
from friday.sentinel.services import HealthState, Services


async def build_services(make_settings, tmp_path, **env) -> Services:
    env.setdefault("FRIDAY_NODE_ID", "sentinel-test")
    settings = make_settings(**env)
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store, poll_interval_s=0.05)
    bus.subscribe(HeartbeatHandler())
    bus.subscribe(TelemetryHandler())
    vault = Vault.from_master_key(settings.master_key)
    config = RuntimeConfig(store, vault, settings, bus)
    await config.load()
    services = Services(
        settings=settings, store=store, bus=bus,
        state=HealthState(started_at=time.time(), platform=detect(), restarts={"x": 2}),
        vault=vault, config=config,
        sessions=SessionManager(store), node_tokens=NodeTokens(store),
        limiter=LoginLimiter(max_failures=3, lockout_s=60.0),
    )
    ctx = HandlerContext(settings=settings, store=store, bus=bus, logger=logging.getLogger("test"))
    services._dispatcher = asyncio.create_task(bus.run_dispatcher(ctx))   # test-only attribute
    return services


async def teardown_services(services: Services) -> None:
    await services.bus.drain()
    await asyncio.wait_for(services._dispatcher, 2)
    await services.store.aclose()


@pytest.fixture
async def services(make_settings, tmp_path):
    s = await build_services(make_settings, tmp_path)
    yield s
    await teardown_services(s)


@pytest.fixture
async def node_token(services) -> str:
    _, token = await services.node_tokens.create("test-node")
    return token


def node_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")
