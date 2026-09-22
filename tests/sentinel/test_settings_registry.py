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
    assert [g["name"] for g in groups] == ["llm", "desktop", "controls"]
    flat = json.dumps(groups)
    assert "value" not in flat
    key = next(k for g in groups for k in g["keys"] if k["key"] == "controls.call_mode")
    assert key["type"] == "enum" and key["choices"] == ["always", "urgent_only", "mute"]
    assert key["secret"] is False and "description" in key
