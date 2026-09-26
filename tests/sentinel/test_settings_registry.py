import json

import pytest

from friday.sentinel.settings_registry import (REGISTRY, SettingSpec, SettingValidationError,
                                               schema, spec_for, validate)


def test_registry_keys_are_unique_and_dotted():
    keys = [s.key for s in REGISTRY]
    assert len(keys) == len(set(keys))
    assert all("." in k and k == k.lower() for k in keys)


def test_initial_registry_contents():
    assert spec_for("llm.gemini_api_key").secret is True
    assert spec_for("llm.gemini_api_key").scopes == ("desktop",)
    assert spec_for("llm.gemini_api_key").env == "GEMINI_API_KEY"
    assert spec_for("llm.routes.triage").scopes == ()
    assert spec_for("llm.routes.widget").default == "gemini:gemini-3.7-flash"
    assert spec_for("controls.call_mode").choices == ("always", "urgent_only", "mute")
    assert spec_for("controls.call_mode").default == "urgent_only"
    assert spec_for("controls.dnd").default is False
    assert spec_for("desktop.voice").env == "FRIDAY_VOICE"
    with pytest.raises(KeyError):
        spec_for("no.such")


@pytest.mark.parametrize("key,raw,expected", [
    ("llm.gemini_api_key", "  abc  ", "abc"),
    ("llm.routes.live", " Gemini : m ", "gemini:m"),
    ("controls.call_mode", "mute", "mute"),
    ("controls.dnd", "true", True),
    ("controls.dnd", False, False),
    ("controls.dnd", "0", False),
])
def test_validate_coerces(key, raw, expected):
    assert validate(spec_for(key), raw) == expected


@pytest.mark.parametrize("key,raw", [
    ("llm.gemini_api_key", 123),
    ("llm.gemini_api_key", ""),
    ("llm.routes.live", "nocolon"),
    ("controls.call_mode", "sometimes"),
    ("controls.dnd", "maybe"),
    ("controls.dnd", 2),
])
def test_validate_rejects(key, raw):
    with pytest.raises(SettingValidationError) as excinfo:
        validate(spec_for(key), raw)
    assert key in str(excinfo.value)


def test_generic_types():
    assert validate(SettingSpec("t.i", "int", "t", ""), "42") == 42
    assert validate(SettingSpec("t.f", "float", "t", ""), "1.5") == 1.5
    assert validate(SettingSpec("t.u", "url", "t", ""), "https://x.example/api") == "https://x.example/api"
    assert validate(SettingSpec("t.l", "list", "t", ""), "a, b,,c") == ["a", "b", "c"]
    assert validate(SettingSpec("t.l", "list", "t", ""), ["x", "y"]) == ["x", "y"]
    for spec, bad in [(SettingSpec("t.i", "int", "t", ""), "x"), (SettingSpec("t.i", "int", "t", ""), True),
                      (SettingSpec("t.u", "url", "t", ""), "ftp://x"), (SettingSpec("t.l", "list", "t", ""), [1])]:
        with pytest.raises(SettingValidationError):
            validate(spec, bad)


def test_schema_has_groups_and_no_values():
    groups = schema()
    assert [g["name"] for g in groups] == ["llm", "desktop", "sentinel", "sources", "voice", "controls"]
    flat = json.dumps(groups)
    assert "value" not in flat
    key = next(k for g in groups for k in g["keys"] if k["key"] == "controls.call_mode")
    assert key["type"] == "enum" and key["choices"] == ["always", "urgent_only", "mute"]
    assert key["secret"] is False and "description" in key


def test_sentinel_and_monitor_keys():
    from friday.sentinel.settings_registry import GROUP_ORDER
    assert GROUP_ORDER == ("llm", "desktop", "sentinel", "sources", "voice", "controls")
    assert spec_for("sentinel.telemetry_interval_s").env == "FRIDAY_TELEMETRY_INTERVAL"
    assert spec_for("sentinel.telemetry_interval_s").default == 15.0
    assert spec_for("sentinel.heartbeat_interval_s").default == 30.0
    assert spec_for("sentinel.retention_days").default == 14
    assert spec_for("sentinel.chat_retention_days").default == 90 and spec_for("sentinel.chat_retention_days").env is None
    for name in ("email", "calendar", "jira"):
        assert spec_for(f"controls.monitors.{name}").type == "bool" and spec_for(f"controls.monitors.{name}").default is False
    assert spec_for("llm.routes.assistant").default == "gemini:gemini-3.7-flash"
    assert spec_for("llm.routes.assistant").scopes == () and spec_for("llm.routes.assistant").env == "FRIDAY_LLM_ASSISTANT"
    assert [g["name"] for g in schema()] == ["llm", "desktop", "sentinel", "sources", "voice", "controls"]


@pytest.mark.parametrize("key,raw,expected", [
    ("sentinel.telemetry_interval_s", "2.5", 2.5),
    ("sentinel.retention_days", "30", 30),
    ("controls.monitors.email", "on", True),
])
def test_new_keys_validate(key, raw, expected):
    assert validate(spec_for(key), raw) == expected


@pytest.mark.parametrize("key,raw", [
    ("sentinel.telemetry_interval_s", "0.5"),
    ("sentinel.telemetry_interval_s", "4000"),
    ("sentinel.retention_days", "0"),
    ("sentinel.chat_retention_days", "9999"),
])
def test_new_keys_reject_out_of_range(key, raw):
    with pytest.raises(SettingValidationError) as excinfo:
        validate(spec_for(key), raw)
    assert "between" in str(excinfo.value)


def test_voice_keys():
    from friday.sentinel.settings_registry import GROUP_ORDER
    assert GROUP_ORDER == ("llm", "desktop", "sentinel", "sources", "voice", "controls")
    assert spec_for("voice.enabled").type == "bool" and spec_for("voice.enabled").default is True
    assert spec_for("voice.name").default == "Aoede"
    assert spec_for("voice.name").choices == ("Aoede", "Kore", "Charon", "Fenrir", "Puck")
    assert validate(spec_for("voice.name"), "Kore") == "Kore"
    with pytest.raises(SettingValidationError):
        validate(spec_for("voice.name"), "Siri")
    assert [g["name"] for g in schema()] == ["llm", "desktop", "sentinel", "sources", "voice", "controls"]


def test_sources_keys():
    from friday.sentinel.settings_registry import GROUP_ORDER
    assert GROUP_ORDER == ("llm", "desktop", "sentinel", "sources", "voice", "controls")
    for key in ("sources.google.client_secret", "sources.google.refresh_token",
                "sources.imap.password", "sources.jira.api_token"):
        assert spec_for(key).secret is True, key
    for key in ("sources.google.client_id", "sources.imap.host", "sources.imap.user",
                "sources.jira.base_url", "sources.jira.email", "sources.jira.jql"):
        assert spec_for(key).secret is False, key
    assert spec_for("sources.imap.port").default == 993
    assert spec_for("sources.imap.drafts_folder").default == "Drafts"
    assert spec_for("sources.email_interval_s").default == 120.0
    assert spec_for("sources.calendar_interval_s").default == 300.0
    assert spec_for("sources.jira_interval_s").default == 300.0
    assert spec_for("sources.calendar_horizon_min").default == 120
    assert spec_for("sources.max_backoff_s").default == 900.0
    from friday.sentinel.sources.jira import DEFAULT_JQL
    assert spec_for("sources.jira.jql").default == DEFAULT_JQL
    assert [g["name"] for g in schema()] == ["llm", "desktop", "sentinel", "sources", "voice", "controls"]


@pytest.mark.parametrize("key,raw", [
    ("sources.email_interval_s", "10"),          # below the 30 s floor
    ("sources.email_interval_s", "99999"),
    ("sources.calendar_horizon_min", "1"),
    ("sources.max_backoff_s", "5"),
    ("sources.imap.port", "70000"),
])
def test_sources_ranges_are_enforced(key, raw):
    with pytest.raises(SettingValidationError):
        validate(spec_for(key), raw)


def test_there_is_no_smtp_setting_anywhere():
    """Structural guarantee: nothing can be configured to send mail."""
    from friday.sentinel.settings_registry import REGISTRY
    assert not [s.key for s in REGISTRY if "smtp" in s.key.lower()]
