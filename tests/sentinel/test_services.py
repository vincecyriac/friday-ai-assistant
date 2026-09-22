import asyncio

import pytest

from friday.core.config import ConfigError
from friday.sentinel import services as services_module


async def test_audit_wrapper_uses_node_id(services):
    await services.audit("user:vince", "logout", None, {})
    events = await services.store.list_events(type="audit.entry")
    assert events[0].event.source == "sentinel-test" and events[0].event.payload["action"] == "logout"


async def test_provider_for_overlays_vault_on_env(services, monkeypatch):
    seen = []
    monkeypatch.setattr(services_module, "get_provider", lambda settings, role: seen.append((settings, role)) or "P")
    assert services.provider_for("assistant") == "P"
    settings, role = seen[0]
    assert role == "assistant" and settings.gemini_api_key is None            # nothing in env or vault
    assert settings.llm_routes["assistant"] == "gemini:gemini-3.7-flash"

    await services.config.set_many({"llm.gemini_api_key": "vault-key-0123456789",
                                    "llm.routes.assistant": "gemini:vault-model"}, actor="t")
    services.provider_for("assistant")
    settings, _ = seen[1]
    assert settings.gemini_api_key == "vault-key-0123456789" and settings.llm_routes["assistant"] == "gemini:vault-model"


async def test_provider_for_without_key_raises_config_error(services):
    with pytest.raises(ConfigError):
        services.provider_for("assistant")


def test_chat_locks_default(services):
    assert services.chat_locks == {}
    lock = services.chat_locks.setdefault("c1", asyncio.Lock())
    assert services.chat_locks["c1"] is lock
