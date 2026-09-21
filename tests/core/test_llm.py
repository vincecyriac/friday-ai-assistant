import asyncio
from types import SimpleNamespace

import pytest

from friday.core.config import ConfigError
from friday.core.llm import gemini_client, get_provider, resolve
from friday.core.llm.base import LLMResponse, ToolCall
from friday.core.llm.gemini import GeminiProvider
from friday.core.llm.routing import Route, parse_route


def test_parse_route():
    assert parse_route("gemini:gemini-3.7-flash") == Route("gemini", "gemini-3.7-flash")
    assert parse_route(" Gemini : m ") == Route("gemini", "m")


@pytest.mark.parametrize("bad", ["", "gemini", ":m", "gemini:", "gemini:   "])
def test_parse_route_bad(bad):
    with pytest.raises(ConfigError) as excinfo:
        parse_route(bad, variable="FRIDAY_LLM_X")
    assert "FRIDAY_LLM_X" in str(excinfo.value)


def test_resolve_roles(make_settings):
    s = make_settings(FRIDAY_LLM_WIDGET="gemini:w")
    assert resolve(s, "widget") == Route("gemini", "w")
    assert resolve(s, "live").model == "gemini-3.1-flash-live-preview"


def test_resolve_unknown_role(make_settings):
    with pytest.raises(ConfigError):
        resolve(make_settings(), "nope")


def test_resolve_bad_spec_names_variable(make_settings):
    s = make_settings(FRIDAY_LLM_TRIAGE="nocolon")
    with pytest.raises(ConfigError) as excinfo:
        resolve(s, "triage")
    assert "FRIDAY_LLM_TRIAGE" in str(excinfo.value)


def test_gemini_client_requires_key(make_settings):
    with pytest.raises(ConfigError) as excinfo:
        gemini_client(make_settings())
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_gemini_client_is_cached_per_key(make_settings):
    s = make_settings(GEMINI_API_KEY="k-test-cache")
    assert gemini_client(s) is gemini_client(s)


def test_get_provider_unknown_provider(make_settings):
    s = make_settings(GEMINI_API_KEY="k", FRIDAY_LLM_TRIAGE="openai:gpt")
    with pytest.raises(ConfigError) as excinfo:
        get_provider(s, "triage")
    assert "openai" in str(excinfo.value) and "FRIDAY_LLM_TRIAGE" in str(excinfo.value)


def test_get_provider_gemini(make_settings):
    p = get_provider(make_settings(GEMINI_API_KEY="k"), "triage")
    assert isinstance(p, GeminiProvider)
    assert p.name == "gemini" and p.model == "gemini-3.7-flash"


class _FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _fake_client(response):
    return SimpleNamespace(aio=SimpleNamespace(models=_FakeModels(response)))


async def test_generate_text():
    response = SimpleNamespace(text="hello", function_calls=None)
    client = _fake_client(response)
    r = await GeminiProvider(client, "m").generate("hi", system="be brief", temperature=0.1)
    assert r == LLMResponse(text="hello", tool_calls=(), raw=response)
    kwargs = client.aio.models.calls[0]
    assert kwargs["model"] == "m" and kwargs["contents"] == "hi"
    assert kwargs["config"].system_instruction == "be brief"
    assert kwargs["config"].temperature == 0.1
    assert kwargs["config"].tools is None


async def test_generate_tool_calls():
    call = SimpleNamespace(name="look", args={"display": "all"})
    client = _fake_client(SimpleNamespace(text=None, function_calls=[call]))
    tools = [{"name": "look", "description": "d",
              "parameters": {"type": "object", "properties": {"display": {"type": "string"}}}}]
    r = await GeminiProvider(client, "m").generate("do", tools=tools)
    assert r.text == ""
    assert r.tool_calls == (ToolCall("look", {"display": "all"}),)
    config = client.aio.models.calls[0]["config"]
    assert config.tools[0].function_declarations[0].name == "look"
    assert config.automatic_function_calling.disable is True


async def test_generate_timeout():
    class Slow:
        async def generate_content(self, **kwargs):
            await asyncio.sleep(1)

    client = SimpleNamespace(aio=SimpleNamespace(models=Slow()))
    with pytest.raises(asyncio.TimeoutError):
        await GeminiProvider(client, "m").generate("x", timeout_s=0.01)
