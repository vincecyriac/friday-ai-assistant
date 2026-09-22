"""Dynamic configuration: the vault fronted by the registry.

Precedence per key is vault → legacy environment → registry default. Secrets
are decrypted on read and never leave this module except through
``view_for_scope`` (for a node pull) — ``view_for_user`` masks them.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping

from friday.core.config import LLM_ROLES, Settings
from friday.core.events import Event
from friday.core.llm.routing import parse_route
from friday.core.storage import AsyncStore, SettingRow
from friday.core.vault import Vault, VaultError
from friday.sentinel.settings_registry import (REGISTRY, SettingSpec, SettingValidationError,
                                               spec_for, validate)

log = logging.getLogger(__name__)

HINT_MIN_LENGTH = 12
_ROUTE_PREFIX = "llm.routes."


def legacy_value(spec: SettingSpec, settings: Settings) -> Any | None:
    """The value a legacy .env variable supplies for ``spec``, or None."""
    if spec.key.startswith(_ROUTE_PREFIX):
        role = spec.key[len(_ROUTE_PREFIX):]
        if role not in LLM_ROLES:
            return None
        raw = settings.env.get(spec.env or "")
        if not raw and role == "live" and settings.gemini_model:
            raw = f"gemini:{settings.gemini_model}"
        if not raw:
            return None
        try:
            route = parse_route(raw)
        except Exception:
            log.warning("ignoring malformed legacy value for %s: %r", spec.key, raw)
            return None
        return f"{route.provider}:{route.model}"
    if spec.env is None:
        return None
    raw = settings.env.get(spec.env)
    return raw.strip() if raw and raw.strip() else None


class RuntimeConfig:
    def __init__(self, store: AsyncStore, vault: Vault, settings: Settings, bus=None):
        self._store = store
        self._vault = vault
        self._settings = settings
        self._bus = bus
        self._rows: dict[str, SettingRow] = {}
        self._decrypted: dict[str, Any] = {}       # key → value or None (undecryptable)
        self._warned: set[str] = set()

    async def load(self) -> None:
        self._rows = {row.key: row for row in await self._store.settings_all()}
        self._decrypted = {}

    # ------------------------------------------------------------------ read

    def _vault_value(self, spec: SettingSpec) -> tuple[bool, Any]:
        """(present, value). value is None when present but undecryptable."""
        row = self._rows.get(spec.key)
        if row is None:
            return False, None
        if spec.key in self._decrypted:
            return True, self._decrypted[spec.key]
        try:
            text = self._vault.decrypt(spec.key, row.value) if row.secret else row.value
            value = text if spec.secret else json.loads(text)
        except (VaultError, ValueError) as e:
            if spec.key not in self._warned:
                self._warned.add(spec.key)
                log.warning("cannot decrypt setting %s (master key changed?): %s", spec.key, e)
            value = None
        self._decrypted[spec.key] = value
        return True, value

    def source(self, key: str) -> str:
        spec = spec_for(key)
        present, value = self._vault_value(spec)
        if present:
            return "vault" if value is not None else "undecryptable"
        if legacy_value(spec, self._settings) is not None:
            return "env"
        return "default"

    def get(self, key: str) -> Any:
        spec = spec_for(key)
        present, value = self._vault_value(spec)
        if present:
            return value
        legacy = legacy_value(spec, self._settings)
        return legacy if legacy is not None else spec.default

    def known_scopes(self) -> set[str]:
        return {scope for spec in REGISTRY for scope in spec.scopes}

    # ----------------------------------------------------------------- write

    async def set_many(self, updates: Mapping[str, Any], *, actor: str) -> None:
        cleaned: dict[str, tuple[SettingSpec, Any]] = {}
        errors: dict[str, str] = {}
        for key, raw in updates.items():
            try:
                spec = spec_for(key)
            except KeyError:
                errors[key] = "unknown setting"
                continue
            try:
                cleaned[key] = (spec, validate(spec, raw))
            except SettingValidationError as e:
                errors[key] = e.errors.get(key, str(e))
        if errors:
            raise SettingValidationError(
                "; ".join(f"{k}: {v}" for k, v in errors.items()), errors)

        now = time.time()
        for key, (spec, value) in cleaned.items():
            stored = self._vault.encrypt(key, str(value)) if spec.secret else json.dumps(value)
            await self._store.setting_set(key, stored, secret=spec.secret, updated_by=actor, ts=now)
            await self._store.audit_append(now, actor, "settings.update", key,
                                           {"secret": True} if spec.secret else {"value": value})
        await self.load()
        await self._publish(sorted(cleaned), actor)

    async def unset(self, key: str, *, actor: str) -> None:
        spec = spec_for(key)
        if await self._store.setting_delete(key):
            now = time.time()
            await self._store.audit_append(now, actor, "settings.unset", key, {"secret": spec.secret})
            await self.load()
            await self._publish([key], actor)

    async def _publish(self, keys: list[str], actor: str) -> None:
        if self._bus is None:
            return
        await self._bus.publish(Event(type="config.changed", source=self._settings.node_id,
                                      payload={"keys": keys, "actor": actor}))

    # ----------------------------------------------------------------- views

    def view_for_user(self) -> list[dict]:
        out: list[dict] = []
        for spec in REGISTRY:
            source = self.source(spec.key)
            if spec.secret:
                value = self.get(spec.key)
                text = "" if value is None else str(value)
                out.append({"key": spec.key, "secret": True, "set": bool(text),
                            "hint": text[-4:] if len(text) >= HINT_MIN_LENGTH else "",
                            "source": source})
            else:
                out.append({"key": spec.key, "secret": False, "value": self.get(spec.key),
                            "source": source})
        return out

    def view_for_scope(self, scope: str) -> dict[str, Any]:
        return {spec.key: self.get(spec.key) for spec in REGISTRY if scope in spec.scopes}
