"""Fire-and-forget event poster from the desktop node to the sentinel.

``post`` never raises and never blocks the voice path for longer than the
request timeout: a sentinel that is down costs one WARNING line, then DEBUG
lines until it is back.
"""

from __future__ import annotations

import logging

import aiohttp

from friday.core.events import Event

log = logging.getLogger(__name__)


class SentinelClient:
    def __init__(self, url: str, token: str | None, node_id: str, *, timeout_s: float = 5.0):
        self._events_url = url.rstrip("/") + "/events"
        self._token = token
        self.node_id = node_id
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._failing = False

    async def post(self, event: Event) -> bool:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            async with self._session.post(self._events_url, json=event.to_dict(),
                                          headers=headers) as resp:
                if resp.status != 202:
                    raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            (log.debug if self._failing else log.warning)(
                "sentinel post to %s failed: %s", self._events_url, e)
            self._failing = True
            return False
        if self._failing:
            log.info("sentinel reachable again at %s", self._events_url)
            self._failing = False
        return True

    async def fetch_config(self, scope: str = "desktop") -> dict | None:
        """Pull this node's configuration. None on any failure (never raises)."""
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = self._events_url[: -len("/events")] + "/config"
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            async with self._session.get(url, params={"scope": scope}, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                body = await resp.json()
        except Exception as e:
            (log.debug if self._failing else log.warning)("sentinel config pull from %s failed: %s", url, e)
            self._failing = True
            return None
        values = body.get("values") if isinstance(body, dict) else None
        return values if isinstance(values, dict) else None

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
