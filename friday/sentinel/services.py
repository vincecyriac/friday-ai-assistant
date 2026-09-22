"""Everything a request handler or monitor may need, in one object."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

from aiohttp import web

from friday.core.config import Settings, apply_overrides
from friday.core.llm import LLMProvider, get_provider
from friday.core.platform import PlatformInfo
from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.audit import record
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
    chat_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def audit(self, actor: str, action: str, target: str | None,
                    detail: Mapping[str, Any]) -> int:
        return await record(self.store, self.bus, self.settings.node_id, actor, action, target, detail)

    def provider_for(self, role: str) -> LLMProvider:
        """An LLM provider for ``role`` with the vault's key and route laid over .env."""
        overlay = {key: self.config.get(key) for key in ("llm.gemini_api_key", f"llm.routes.{role}")}
        return get_provider(apply_overrides(self.settings, overlay), role)


SERVICES = web.AppKey("services", Services)
