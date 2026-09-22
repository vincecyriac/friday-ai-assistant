import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from friday.core.storage import AsyncStore
from friday.core.vault import Vault
from friday.sentinel.bus import EventBus
from friday.sentinel.handlers import HandlerContext
from friday.sentinel.runtime_config import RuntimeConfig, legacy_value
from friday.sentinel.settings_registry import SettingValidationError, spec_for


@pytest.fixture
async def rig(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key", GEMINI_MODEL="env-live-model", FRIDAY_LLM_WIDGET="gemini:env-widget")
    store = await AsyncStore.open(tmp_path / "t.db")
    bus = EventBus(store)
    vault = Vault.from_master_key(settings.master_key)
    config = RuntimeConfig(store, vault, settings, bus)
    await config.load()
    yield SimpleNamespace(settings=settings, store=store, bus=bus, vault=vault, config=config)
    await store.aclose()


def test_legacy_value(make_settings):
    s = make_settings(GEMINI_API_KEY="k", GEMINI_MODEL="live-m", FRIDAY_LLM_WIDGET="gemini:w")
    assert legacy_value(spec_for("llm.gemini_api_key"), s) == "k"
    assert legacy_value(spec_for("llm.routes.live"), s) == "gemini:live-m"
    assert legacy_value(spec_for("llm.routes.widget"), s) == "gemini:w"
    assert legacy_value(spec_for("llm.routes.triage"), s) is None
    assert legacy_value(spec_for("controls.dnd"), s) is None
    bad = make_settings(FRIDAY_LLM_TRIAGE="nocolon")
    assert legacy_value(spec_for("llm.routes.triage"), bad) is None      # malformed legacy → ignored


async def test_precedence_vault_env_default(rig):
    c = rig.config
    assert c.get("llm.gemini_api_key") == "env-key" and c.source("llm.gemini_api_key") == "env"
    assert c.get("llm.routes.live") == "gemini:env-live-model" and c.source("llm.routes.live") == "env"
    assert c.get("llm.routes.triage") == "gemini:gemini-3.7-flash" and c.source("llm.routes.triage") == "default"
    assert c.get("controls.dnd") is False and c.source("controls.dnd") == "default"

    await c.set_many({"llm.gemini_api_key": "vault-key", "controls.dnd": "true"}, actor="test")
    assert c.get("llm.gemini_api_key") == "vault-key" and c.source("llm.gemini_api_key") == "vault"
    assert c.get("controls.dnd") is True and c.source("controls.dnd") == "vault"


async def test_secrets_are_encrypted_at_rest_and_never_audited(rig):
    await rig.config.set_many({"llm.gemini_api_key": "hunter2hunter2"}, actor="dashboard:vince")
    row = await rig.store.setting_get("llm.gemini_api_key")
    assert row.value.startswith("v1:") and "hunter2" not in row.value and row.secret is True
    assert rig.vault.decrypt("llm.gemini_api_key", row.value) == "hunter2hunter2"
    audit = await rig.store.audit_list()
    assert audit[0].action == "settings.update" and audit[0].target == "llm.gemini_api_key"
    assert audit[0].actor == "dashboard:vince"
    assert "hunter2" not in str(audit[0].detail) and audit[0].detail["secret"] is True


async def test_non_secret_stored_as_json(rig):
    await rig.config.set_many({"controls.call_mode": "mute"}, actor="test")
    row = await rig.store.setting_get("controls.call_mode")
    assert row.value == '"mute"' and row.secret is False


async def test_set_many_is_all_or_nothing(rig):
    with pytest.raises(SettingValidationError) as excinfo:
        await rig.config.set_many({"controls.dnd": "true", "controls.call_mode": "sometimes", "no.such": 1},
                                  actor="test")
    message = str(excinfo.value)
    assert "controls.call_mode" in message and "no.such" in message
    assert rig.config.source("controls.dnd") == "default"
    assert await rig.store.settings_all() == []


async def test_config_changed_is_published(rig):
    await rig.config.set_many({"controls.dnd": True, "controls.call_mode": "always"}, actor="test")
    rows = await rig.store.list_events(type="config.changed")
    assert len(rows) == 1
    assert sorted(rows[0].event.payload["keys"]) == ["controls.call_mode", "controls.dnd"]
    assert rows[0].event.payload["actor"] == "test"


async def test_unset_returns_to_env_or_default(rig):
    await rig.config.set_many({"llm.gemini_api_key": "vault-key", "controls.dnd": True}, actor="test")
    await rig.config.unset("llm.gemini_api_key", actor="test")
    await rig.config.unset("controls.dnd", actor="test")
    assert rig.config.get("llm.gemini_api_key") == "env-key" and rig.config.source("llm.gemini_api_key") == "env"
    assert rig.config.get("controls.dnd") is False
    await rig.config.unset("controls.dnd", actor="test")          # idempotent


async def test_views(rig):
    await rig.config.set_many({"llm.gemini_api_key": "abcdefghijklmnop", "llm.tripo_api_key": "short"}, actor="t")
    view = {item["key"]: item for item in rig.config.view_for_user()}
    assert view["llm.gemini_api_key"] == {"key": "llm.gemini_api_key", "secret": True, "set": True,
                                          "hint": "mnop", "source": "vault"}
    assert view["llm.tripo_api_key"]["hint"] == ""                    # too short for a hint
    assert view["llm.routes.triage"] == {"key": "llm.routes.triage", "secret": False,
                                         "value": "gemini:gemini-3.7-flash", "source": "default"}
    assert "abcdefgh" not in str(rig.config.view_for_user())

    desktop = rig.config.view_for_scope("desktop")
    assert desktop["llm.gemini_api_key"] == "abcdefghijklmnop"
    assert desktop["llm.routes.widget"] == "gemini:env-widget"
    assert "llm.routes.triage" not in desktop and "controls.dnd" not in desktop
    assert rig.config.known_scopes() == {"desktop"}


async def test_undecryptable_secret_is_treated_as_unset(rig, caplog):
    other = Vault.from_master_key(Vault.generate_master_key())
    await rig.store.setting_set("llm.tripo_api_key", other.encrypt("llm.tripo_api_key", "x"),
                                secret=True, updated_by="old", ts=time.time())
    await rig.config.load()
    with caplog.at_level(logging.WARNING):
        assert rig.config.get("llm.tripo_api_key") is None
        assert rig.config.source("llm.tripo_api_key") == "undecryptable"
        rig.config.get("llm.tripo_api_key")
    assert sum("cannot decrypt" in r.getMessage() for r in caplog.records) == 1
    view = {item["key"]: item for item in rig.config.view_for_user()}
    assert view["llm.tripo_api_key"]["source"] == "undecryptable" and view["llm.tripo_api_key"]["set"] is False


async def test_settings_writes_publish_audit_entry(rig):
    await rig.config.set_many({"controls.dnd": True}, actor="dashboard:vince")
    await rig.config.unset("controls.dnd", actor="dashboard:vince")
    entries = await rig.store.list_events(type="audit.entry")
    assert [e.event.payload["action"] for e in entries] == ["settings.unset", "settings.update"]
    assert entries[0].event.payload["actor"] == "dashboard:vince"
