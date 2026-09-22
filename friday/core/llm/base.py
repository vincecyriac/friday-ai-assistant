"""Provider-neutral text generation interface.

Tools are plain JSON-schema function declarations
(``{"name", "description", "parameters"}``) — the shape the hub already
keeps in TOOL_FUNCTION_DECLARATIONS — so a second provider maps the same
input. The Gemini Live audio session is not behind this interface; it is a
provider-specific protocol and stays in friday.desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any


@dataclass(frozen=True)
class Message:
    """One turn of a conversation. ``tool`` rows carry a tool's output back to the
    model; ``assistant`` rows may carry the calls the model requested."""
    role: str                                  # "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None
    tool_result: str | None = None


@dataclass(frozen=True)
class Chunk:
    """A streaming increment: text, a requested tool call, or the end of the turn."""
    kind: str                                  # "text" | "tool_call" | "end"
    text: str = ""
    tool_call: ToolCall | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str, *, system: str | None = None,
                       tools: Sequence[Mapping[str, Any]] | None = None,
                       temperature: float | None = None,
                       timeout_s: float = 60.0) -> LLMResponse: ...

    def stream(self, messages: Sequence[Message], *, system: str | None = None,
               tools: Sequence[Mapping[str, Any]] | None = None,
               temperature: float | None = None,
               timeout_s: float = 60.0) -> AsyncIterator[Chunk]: ...
