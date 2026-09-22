"""Overlay the sentinel's configuration onto local Settings, with a cache.

Order of preference: a live pull, then the last good pull cached on disk,
then the local .env as loaded. The cache is written with mode 0600 — it
holds the same secrets the plaintext .env it supersedes did.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from friday.core.config import Settings, apply_overrides

log = logging.getLogger(__name__)


def _write_cache(path: Path, values: dict[str, Any]) -> None:
    payload = json.dumps({"values": values, "fetched_at": time.time()}, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    os.chmod(path, 0o600)


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    values = data.get("values") if isinstance(data, dict) else None
    return values if isinstance(values, dict) else None


async def pull_or_cached(settings: Settings, fetch: Callable[[], Awaitable[dict | None]],
                         cache_path: Path) -> tuple[Settings, str]:
    values = await fetch()
    if values is not None:
        try:
            _write_cache(cache_path, values)
        except OSError as e:
            log.warning("could not write config cache %s: %s", cache_path, e)
        return apply_overrides(settings, values), "sentinel"
    cached = _read_cache(cache_path)
    if cached is not None:
        return apply_overrides(settings, cached), "cache"
    return settings, "env"
