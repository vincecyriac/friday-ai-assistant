"""The sentinel process: boot, supervise, shut down in order.

Runs the same on macOS (foreground or launchd) and Linux (systemd,
``Type=notify``). Signals are installed before anything else is started so
a SIGTERM during a slow boot still exits in order; every long-running task
is supervised so one crash never takes the daemon down silently.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

import friday
from friday.core.config import ConfigError, Settings
from friday.core.events import Event
from friday.core.logsetup import configure_logging
from friday.core.platform import detect
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.api import ApiServer
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.bridges import Bridge, load_bridges
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import (HandlerContext, HeartbeatHandler, LogHandler,
                                      TelemetryHandler)
from friday.sentinel.monitors import Housekeeping, SelfHeartbeat, TelemetryMonitor
from friday.sentinel.runtime_config import RuntimeConfig
from friday.sentinel.sdnotify import sd_notify
from friday.sentinel.services import HealthState, Services
from friday.sentinel.watch import WatchRunner


class Sentinel:
    RESTART_BACKOFF_BASE_S = 2.0
    RESTART_BACKOFF_MAX_S = 30.0
    SHUTDOWN_TIMEOUT_S = 10.0
    BRIDGE_STOP_TIMEOUT_S = 3.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ready = asyncio.Event()
        self.api_port: int | None = None
        self.services: Services | None = None
        self.watch: WatchRunner | None = None
        self.state = HealthState(started_at=time.time(), platform=detect())
        self._shutdown = asyncio.Event()
        self._reason = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log = logging.getLogger("friday.sentinel")

    # ------------------------------------------------------------ control

    def request_shutdown(self, reason: str = "") -> None:
        """Begin an orderly shutdown. Safe to call from any thread or a signal handler."""
        if self._shutdown.is_set():
            return
        self._reason = reason or self._reason
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._shutdown.set)
        else:
            self._shutdown.set()

    # --------------------------------------------------------------- run

    async def run(self) -> int:
        s = self.settings
        configure_logging(s.log_level)
        log = self._log
        self._loop = asyncio.get_running_loop()
        self.state.started_at = time.time()
        info = self.state.platform
        log.info("FRIDAY sentinel %s starting: %s (%s, %s) node=%s data=%s",
                 friday.__version__, info.summary, info.hostname,
                 info.distro or "no distro info", s.node_id, s.data_dir)
        self._install_signal_handlers()

        store: AsyncStore | None = None
        bus: EventBus | None = None
        server: ApiServer | None = None
        bridges: list[Bridge] = []
        monitor_tasks: list[asyncio.Task] = []
        dispatcher_task: asyncio.Task | None = None
        config_error: ConfigError | None = None
        exit_code = 0

        try:
            store = await AsyncStore.open(s.data_dir / "sentinel.db", synchronous=s.db_synchronous)
            requeued = await store.requeue_stale(0.0, time.time())
            if requeued:
                log.warning("requeued %d event(s) orphaned by a previous run", requeued)

            vault = Vault.from_master_key(s.master_key or "")
            log.info("vault open (key fingerprint %s)", vault.fingerprint())

            bus = EventBus(store)
            ctx = HandlerContext(settings=s, store=store, bus=bus,
                                 logger=logging.getLogger("friday.sentinel.events"))
            for handler in (HeartbeatHandler(), TelemetryHandler(), LogHandler()):
                bus.subscribe(handler)

            config = RuntimeConfig(store, vault, s, bus)
            await config.load()
            services = Services(settings=s, store=store, bus=bus, state=self.state, vault=vault,
                                config=config, sessions=SessionManager(store),
                                node_tokens=NodeTokens(store), limiter=LoginLimiter())
            self.services = services
            if await store.users_count() == 0:
                log.warning("no dashboard user exists; create one with: "
                            "friday-sentinel user set-password <name>")

            for bridge in load_bridges(s):
                try:
                    await bridge.start(bus, ctx)
                    bridges.append(bridge)
                    log.info("bridge %s started", bridge.name)
                except Exception:
                    log.exception("bridge %s failed to start; skipping it",
                                  getattr(bridge, "name", bridge))

            server = ApiServer(services)
            await server.start()
            self.api_port = server.port
            log.info("API listening on http://%s:%s", s.sentinel_bind_host, server.port)

            watch_runner = WatchRunner(services)
            self.watch = watch_runner
            services.watch = watch_runner
            for monitor in (TelemetryMonitor(services.config),
                            SelfHeartbeat(services.config),
                            Housekeeping(services.config),
                            watch_runner):
                monitor_tasks.append(asyncio.create_task(
                    self._supervise(monitor.name, lambda m=monitor: m.run(ctx), log),
                    name=f"monitor:{monitor.name}"))
            dispatcher_task = asyncio.create_task(
                self._supervise("dispatcher", lambda: bus.run_dispatcher(ctx), log),
                name="dispatcher")

            await bus.publish(Event(type="sentinel.started", source=s.node_id,
                                    payload={"version": friday.__version__,
                                             "platform": info.to_dict()}))
            sd_notify("READY=1")
            self.ready.set()
            log.info("ready")

            await self._shutdown.wait()
            log.info("shutting down (%s)", self._reason or "no reason given")
        except ConfigError as e:
            config_error = e
            exit_code = 1
        except Exception:
            log.exception("fatal error during boot")
            exit_code = 1
        finally:
            await self._teardown(store, bus, server, bridges, monitor_tasks, dispatcher_task)
            self._remove_signal_handlers()
            log.info("stopped")

        if config_error is not None:
            raise config_error
        return exit_code

    # ---------------------------------------------------------- teardown

    async def _teardown(self, store, bus, server, bridges, monitor_tasks, dispatcher_task) -> None:
        log = self._log

        async def ordered() -> None:
            sd_notify("STOPPING=1")
            if bus is not None:
                try:
                    await bus.publish(Event(type="sentinel.stopping", source=self.settings.node_id,
                                            payload={"reason": self._reason}))
                except Exception as e:
                    log.warning("could not record sentinel.stopping: %s", e)
            if server is not None:
                await server.stop()
            for task in monitor_tasks:
                task.cancel()
            if monitor_tasks:
                await asyncio.gather(*monitor_tasks, return_exceptions=True)
            for bridge in bridges:
                try:
                    await asyncio.wait_for(bridge.stop(), self.BRIDGE_STOP_TIMEOUT_S)
                except Exception as e:
                    log.warning("bridge %s did not stop cleanly: %s", bridge.name, e)
            if bus is not None:
                await bus.drain()
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                await asyncio.gather(dispatcher_task, return_exceptions=True)

        try:
            await asyncio.wait_for(ordered(), self.SHUTDOWN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("shutdown exceeded %.0fs; cancelling what is left", self.SHUTDOWN_TIMEOUT_S)
            for task in [*monitor_tasks, dispatcher_task]:
                if task is not None:
                    task.cancel()
        if store is not None:
            try:
                await store.aclose()
            except Exception:
                log.exception("store close failed")

    # --------------------------------------------------------- supervise

    async def _supervise(self, name: str, factory, log: logging.Logger) -> None:
        """Run ``factory()`` to completion, restarting with backoff if it raises."""
        failures = 0
        while not self._shutdown.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                self.state.restarts[name] = self.state.restarts.get(name, 0) + 1
                delay = min(self.RESTART_BACKOFF_MAX_S,
                            self.RESTART_BACKOFF_BASE_S * (2 ** (failures - 1)))
                log.exception("%s crashed; restarting in %.3gs", name, delay)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    # ----------------------------------------------------------- signals

    def _install_signal_handlers(self) -> None:
        loop = self._loop
        assert loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)
            except (NotImplementedError, RuntimeError, ValueError, AttributeError) as e:
                self._log.debug("cannot install handler for %s: %s", sig.name, e)
        hup = getattr(signal, "SIGHUP", None)
        if hup is not None:
            try:
                loop.add_signal_handler(
                    hup, lambda: self._log.info("SIGHUP received; reload not supported, ignoring"))
            except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                pass

    def _remove_signal_handlers(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                pass
