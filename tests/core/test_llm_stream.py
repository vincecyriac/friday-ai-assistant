import asyncio
from types import SimpleNamespace

import pytest
from google.genai import types

from friday.core.llm import Chunk, Message, ToolCall
from friday.core.llm.gemini import GeminiProvider, to_contents


def _response(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)))])


def _fake_client(responses, *, delay_s=0.0, captured=None):
    async def generate_content_stream(*, model, contents, config):
        if captured is not None:
            captured.append({"model": model, "contents": contents, "config": config})

        async def gen():
            for r in responses:
                if delay_s:
                    await asyncio.sleep(delay_s)
                yield r
        return gen()
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate_content_stream)))


def test_to_contents_maps_roles_and_groups_tool_results():
    messages = [
        Message("user", "hi"),
        Message("assistant", "checking", tool_calls=(ToolCall("get_nodes", {}), ToolCall("get_queue", {"x": 1}))),
        Message("tool", tool_name="get_nodes", tool_result="[]"),
        Message("tool", tool_name="get_queue", tool_result="{}"),
        Message("assistant", "done"),
    ]
    contents = to_contents(messages)
    assert [c.role for c in contents] == ["user", "model", "user", "model"]
    assert contents[0].parts[0].text == "hi"
    model_turn = contents[1].parts
    assert model_turn[0].text == "checking"
    assert [p.function_call.name for p in model_turn[1:]] == ["get_nodes", "get_queue"]
    assert dict(model_turn[2].function_call.args) == {"x": 1}
    tool_turn = contents[2].parts
    assert [p.function_response.name for p in tool_turn] == ["get_nodes", "get_queue"]
    assert tool_turn[0].function_response.response == {"output": "[]"}
    with pytest.raises(ValueError):
        to_contents([Message("system", "no")])


async def test_stream_yields_text_then_tool_calls_then_end():
    captured = []
    client = _fake_client([
        _response(types.Part(text="Two ")),
        _response(types.Part(text="nodes."), types.Part(function_call=types.FunctionCall(name="get_nodes", args={}))),
    ], captured=captured)
    provider = GeminiProvider(client, "m")
    chunks = [c async for c in provider.stream([Message("user", "how many nodes?")], system="sys",
                                               tools=[{"name": "get_nodes", "description": "d",
                                                       "parameters": {"type": "object", "properties": {}}}])]
    assert [c.kind for c in chunks] == ["text", "text", "tool_call", "end"]
    assert "".join(c.text for c in chunks if c.kind == "text") == "Two nodes."
    assert chunks[2].tool_call == ToolCall("get_nodes", {})
    call = captured[0]
    assert call["model"] == "m" and call["config"].system_instruction == "sys"
    assert call["config"].automatic_function_calling.disable is True
    assert call["contents"][0].parts[0].text == "how many nodes?"


async def test_stream_without_tools_sends_no_tool_config():
    captured = []
    provider = GeminiProvider(_fake_client([_response(types.Part(text="ok"))], captured=captured), "m")
    assert [c.kind for c in await _collect(provider.stream([Message("user", "x")]))] == ["text", "end"]
    assert captured[0]["config"].tools is None


async def test_stream_timeout_propagates():
    provider = GeminiProvider(_fake_client([_response(types.Part(text="slow"))], delay_s=0.5), "m")
    with pytest.raises(asyncio.TimeoutError):
        await _collect(provider.stream([Message("user", "x")], timeout_s=0.05))


async def test_stream_skips_empty_parts():
    provider = GeminiProvider(_fake_client([
        types.GenerateContentResponse(candidates=[]),
        _response(types.Part(text="")),
        _response(types.Part(text="a")),
    ]), "m")
    chunks = await _collect(provider.stream([Message("user", "x")]))
    assert [c.kind for c in chunks] == ["text", "end"] and chunks[0].text == "a"


async def _collect(aiter):
    return [c async for c in aiter]
