"""Gemini adapter over google-genai's generate_content."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from google.genai import types

from friday.core.llm.base import LLMResponse, ToolCall


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: Any, model: str):
        self._client = client
        self.model = model

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse:
        config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        if tools:
            config.tools = [types.Tool(function_declarations=[
                types.FunctionDeclaration(**dict(tool)) for tool in tools])]
            # The caller owns tool execution; the SDK must not call anything itself.
            config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(
                model=self.model, contents=prompt, config=config),
            timeout=timeout_s)
        calls = tuple(ToolCall(name=c.name, args=dict(c.args or {}))
                      for c in (getattr(response, "function_calls", None) or []))
        return LLMResponse(text=getattr(response, "text", None) or "",
                           tool_calls=calls, raw=response)
