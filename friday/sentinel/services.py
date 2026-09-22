"""Everything a request handler or monitor may need, in one object."""

from __future__ import annotations

from dataclasses import dataclass, field

from aiohttp import web

from friday.core.config import Settings
from friday.core.platform import PlatformInfo
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.auth import LoginLimiter, NodeTokens, SessionManager
from friday.sentinel.bus import EventBus
from friday.sentinel.runtime_config import RuntimeConfig


@dataclass
class HealthState:
    started_at: float
    platform: PlatformInfo
    restarts: dict[str, int] = field(default_factory=dict)


@dataclass
class Services:
    settings: Settings
    store: AsyncStore
    bus: EventBus
    state: HealthState
    vault: Vault
    config: RuntimeConfig
    sessions: SessionManager
    node_tokens: NodeTokens
    limiter: LoginLimiter


SERVICES = web.AppKey("services", Services)
