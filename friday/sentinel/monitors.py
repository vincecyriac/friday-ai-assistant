"""Periodic tasks the daemon supervises. Each ``run`` loops until cancelled."""

from __future__ import annotations

import asyncio
import time
from functools import partial

import psutil

import friday
from friday.core.events import Event, Heartbeat
from friday.core.platform import detect
from friday.core.telemetry import collect
from friday.sentinel.handlers import HandlerContext
from friday.sentinel.sdnotify import sd_notify


class TelemetryMonitor:
    name = "telemetry"

    def __init__(self, config):
        self.config = config            # RuntimeConfig; read every tick so edits apply live

    async def run(self, ctx: HandlerContext) -> None:
        # cpu_percent measures since its previous call; the first call is always 0.
        psutil.cpu_percent(interval=None)
        loop = asyncio.get_running_loop()
        settings = ctx.settings
        while True:
            snapshot = await loop.run_in_executor(
                None, partial(collect, settings.node_id, settings.data_dir))
            await ctx.bus.publish(Event(type="telemetry.sample", source=settings.node_id,
                                        payload=snapshot.to_dict()))
            await asyncio.sleep(float(self.config.get("sentinel.telemetry_interval_s")))


class SelfHeartbeat:
    name = "heartbeat"

    def __init__(self, config):
        self.config = config

    async def run(self, ctx: HandlerContext) -> None:
        platform = detect().summary
        node_id = ctx.settings.node_id
        while True:
            hb = Heartbeat(status="running", version=friday.__version__, platform=platform)
            await ctx.bus.publish(Event(type="node.heartbeat", source=node_id, payload=hb.to_dict()))
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(float(self.config.get("sentinel.heartbeat_interval_s")))


class Housekeeping:
    name = "housekeeping"

    def __init__(self, config, interval_s: float = 3600.0):
        self.config = config
        self.interval_s = interval_s

    async def run(self, ctx: HandlerContext) -> None:
        while True:
            await asyncio.sleep(self.interval_s)          # nothing to prune at boot
            now = time.time()
            retention_days = float(self.config.get("sentinel.retention_days"))
            chat_days = float(self.config.get("sentinel.chat_retention_days"))
            counts = await ctx.store.prune(now - retention_days * 86400)
            sessions = await ctx.store.sessions_prune(now)
            chats = await ctx.store.conversations_prune(now - chat_days * 86400)
            await ctx.store.checkpoint("PASSIVE")
            ctx.logger.info("housekeeping: pruned %d telemetry rows, %d events, %d sessions, %d chats",
                            counts["telemetry"], counts["events"], sessions, chats)
