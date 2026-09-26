"""Google OAuth: a refresh token in, a short-lived access token out.

Only the scopes FRIDAY needs are ever requested. The compose scope is granted
now rather than later so that sub-project 4b's draft replies need no second
consent — the protection against sending is that no such path exists, which an
AST test enforces.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable

from friday.sentinel.sources.base import AuthExpired, SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
)
REFRESH_MARGIN_S = 60.0
TIMEOUT_S = 20.0


class GoogleOAuth:
    def __init__(self, session: Any, client_id: str, client_secret: str, refresh_token: str, *,
                 token_url: str = TOKEN_URL, clock: Callable[[], float] | None = None) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_url = token_url
        self._clock = clock or time.monotonic
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def credentials_key(self) -> str:
        """Identifies this credential set without revealing it — used to decide
        whether a cached client may be reused after a vault edit."""
        material = f"{self._client_id}:{self._refresh_token}".encode()
        return hashlib.sha256(material).hexdigest()[:16]

    async def token(self) -> str:
        async with self._lock:
            if self._access is not None and self._clock() < self._expires_at - REFRESH_MARGIN_S:
                return self._access
            payload = {"grant_type": "refresh_token", "refresh_token": self._refresh_token,
                       "client_id": self._client_id, "client_secret": self._client_secret}
            try:
                async with asyncio.timeout(TIMEOUT_S):
                    async with self._session.post(self._token_url, data=payload) as resp:
                        status, body = resp.status, await resp.json(content_type=None)
            except asyncio.TimeoutError as e:
                raise SourceError("google token request timed out") from e
            except Exception as e:                       # network-level failure
                raise SourceError(f"google token request failed: {type(e).__name__}") from e
            if status in (400, 401):
                # invalid_grant: the refresh token is revoked or expired. Retrying
                # cannot fix it; only re-consent can.
                raise AuthExpired("google refused the refresh token; "
                                  "run 'friday-sentinel google-auth'")
            if status >= 300:
                raise SourceError(f"google token request returned HTTP {status}")
            access = (body or {}).get("access_token")
            if not access:
                raise SourceError("google token response contained no access_token")
            self._access = access
            self._expires_at = self._clock() + float((body or {}).get("expires_in") or 3600)
            return access
