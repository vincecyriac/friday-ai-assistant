"""Role → (provider, model) routing, driven by FRIDAY_LLM_<ROLE> env vars."""

from __future__ import annotations

from dataclasses import dataclass

from friday.core.config import LLM_ROLES, ConfigError, Settings


@dataclass(frozen=True)
class Route:
    provider: str
    model: str


def parse_route(spec: str, *, variable: str = "FRIDAY_LLM") -> Route:
    provider, sep, model = (spec or "").partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not sep or not provider or not model:
        raise ConfigError(f"{variable} must be 'provider:model', got {spec!r}")
    return Route(provider, model)


def resolve(settings: Settings, role: str) -> Route:
    if role not in LLM_ROLES:
        raise ConfigError(f"unknown LLM role {role!r}; expected one of {LLM_ROLES}")
    return parse_route(settings.llm_routes[role], variable=f"FRIDAY_LLM_{role.upper()}")
