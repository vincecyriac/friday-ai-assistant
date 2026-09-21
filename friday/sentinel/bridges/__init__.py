"""Bridges: long-lived adapters between the bus and the outside world.

A bridge is started with the bus and may both publish inbound events
(an SMS arriving -> ``message.received``) and subscribe handlers for the
``command.*`` events it can act on (``command.sms.send``). The sentinel
knows nothing about what a bridge does; ``FRIDAY_BRIDGES`` names the
classes to load. None ship yet — this is the seam for Termux telephony,
WhatsApp, MQTT and cloud voice.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Protocol

from friday.core.config import ConfigError, Settings


class Bridge(Protocol):
    name: str

    async def start(self, bus: Any, ctx: Any) -> None: ...

    async def stop(self) -> None: ...


def load_bridges(settings: Settings) -> list[Bridge]:
    bridges: list[Bridge] = []
    for path in settings.bridges:
        module_name, _, attr = path.rpartition(".")
        if not module_name or not attr:
            raise ConfigError(f"FRIDAY_BRIDGES: {path!r} is not a dotted class path")
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"FRIDAY_BRIDGES: cannot load {path!r}: {e}") from e
        if not inspect.isclass(cls):
            raise ConfigError(f"FRIDAY_BRIDGES: {path!r} is not a class")
        bridges.append(cls())
    return bridges
