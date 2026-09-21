"""LLM factory: resolve a role to a provider+model from the environment.

Only Gemini ships. ``get_provider`` is the seam a second provider plugs into;
callers never construct a provider directly.
"""

from __future__ import annotations

from typing import Any

from friday.core.config import ConfigError, Settings
from friday.core.llm.base import LLMProvider, LLMResponse, ToolCall
from friday.core.llm.routing import Route, parse_route, resolve

__all__ = ["LLMProvider", "LLMResponse", "Route", "ToolCall", "gemini_client",
           "get_provider", "parse_route", "resolve"]

_gemini_clients: dict[str, Any] = {}


def gemini_client(settings: Settings) -> Any:
    """The process-wide google-genai client (one per API key)."""
    key = settings.gemini_api_key
    if not key:
        raise ConfigError("GEMINI_API_KEY is not set")
    if key not in _gemini_clients:
        from google import genai   # deferred: keeps `import friday.core.llm` cheap
        _gemini_clients[key] = genai.Client(api_key=key)
    return _gemini_clients[key]


def get_provider(settings: Settings, role: str) -> LLMProvider:
    route = resolve(settings, role)
    if route.provider == "gemini":
        from friday.core.llm.gemini import GeminiProvider
        return GeminiProvider(gemini_client(settings), route.model)
    raise ConfigError(
        f"FRIDAY_LLM_{role.upper()}: unknown provider {route.provider!r} (supported: gemini)")
