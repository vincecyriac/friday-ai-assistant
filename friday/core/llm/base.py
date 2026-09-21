"""Provider-neutral text generation interface.

Tools are plain JSON-schema function declarations
(``{"name", "description", "parameters"}``) — the shape the hub already
keeps in TOOL_FUNCTION_DECLARATIONS — so a second provider maps the same
input. The Gemini Live audio session is not behind this interface; it is a
provider-specific protocol and stays in friday.desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse: ...
