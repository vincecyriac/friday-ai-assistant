"""Gemini adapter over google-genai's generate_content / generate_content_stream."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Sequence

from google.genai import types

from friday.core.llm.base import Chunk, LLMResponse, Message, ToolCall


def _config(system: str | None, tools: Sequence[Mapping[str, Any]] | None,
            temperature: float | None) -> types.GenerateContentConfig:
    config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
    if tools:
        config.tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(**dict(tool)) for tool in tools])]
        # The caller owns tool execution; the SDK must not call anything itself.
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
    return config


def to_contents(messages: Sequence[Message]) -> list[types.Content]:
    """Map provider-neutral messages onto Gemini's Content list. Consecutive tool
    rows collapse into one user turn of function responses, as the API requires."""
    contents: list[types.Content] = []
    pending: list[types.Part] = []

    def flush() -> None:
        if pending:
            contents.append(types.Content(role="user", parts=list(pending)))
            pending.clear()

    for m in messages:
        if m.role == "tool":
            pending.append(types.Part.from_function_response(
                name=m.tool_name or "", response={"output": m.tool_result or ""}))
            continue
        flush()
        if m.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=m.content)]))
        elif m.role == "assistant":
            parts: list[types.Part] = []
            if m.content:
                parts.append(types.Part.from_text(text=m.content))
            for call in m.tool_calls:
                parts.append(types.Part(function_call=types.FunctionCall(name=call.name, args=dict(call.args))))
            if not parts:
                parts.append(types.Part.from_text(text=""))
            contents.append(types.Content(role="model", parts=parts))
        else:
            raise ValueError(f"unsupported message role {m.role!r}")
    flush()
    return contents


def _parts(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates or candidates[0].content is None:
        return []
    return list(candidates[0].content.parts or [])


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: Any, model: str):
        self._client = client
        self.model = model

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse:
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(
                model=self.model, contents=prompt, config=_config(system, tools, temperature)),
            timeout=timeout_s)
        calls = tuple(ToolCall(name=c.name, args=dict(c.args or {}))
                      for c in (getattr(response, "function_calls", None) or []))
        return LLMResponse(text=getattr(response, "text", None) or "",
                           tool_calls=calls, raw=response)

    async def stream(self, messages: Sequence[Message], *, system: str | None = None,
                     tools: Sequence[Mapping[str, Any]] | None = None,
                     temperature: float | None = None,
                     timeout_s: float = 60.0) -> AsyncIterator[Chunk]:
        """Text chunks as they arrive; tool calls after the stream ends (Gemini
        delivers them whole); then exactly one ``end``. The timeout covers the
        whole stream and raises asyncio.TimeoutError to the caller."""
        calls: list[ToolCall] = []
        async with asyncio.timeout(timeout_s):
            aiter = await self._client.aio.models.generate_content_stream(
                model=self.model, contents=to_contents(messages),
                config=_config(system, tools, temperature))
            async for response in aiter:
                for part in _parts(response):
                    call = getattr(part, "function_call", None)
                    if call is not None and call.name:
                        calls.append(ToolCall(name=call.name, args=dict(call.args or {})))
                    elif getattr(part, "text", None):
                        yield Chunk("text", text=part.text)
        for call in calls:
            yield Chunk("tool_call", tool_call=call)
        yield Chunk("end")
