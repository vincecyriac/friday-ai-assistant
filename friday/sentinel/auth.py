"""Passwords, sessions, node tokens and login throttling. Pure of aiohttp
routing — request handling lives in principals.py / web.py."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from friday.core.storage import AsyncStore, NodeTokenRow

SESSION_COOKIE = "friday_session"
NODE_TOKEN_PREFIX = "fn_"
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 15, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024


@dataclass(frozen=True)
class User:
    username: str


@dataclass(frozen=True)
class Node:
    id: str
    name: str


Principal = User | Node


# -------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ----------------------------------------------------------------- tokens

def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionManager:
    def __init__(self, store: AsyncStore, ttl_s: float = 30 * 86400.0):
        self._store = store
        self.ttl_s = ttl_s

    async def create(self, username: str, *, user_agent: str | None, ip: str | None) -> str:
        token = new_token()
        now = time.time()
        await self._store.session_create(token_hash(token), username, now, now + self.ttl_s,
                                         user_agent, ip)
        return token

    async def resolve(self, token: str) -> User | None:
        row = await self._store.session_get(token_hash(token))
        if row is None:
            return None
        now = time.time()
        if row.expires_at <= now:
            await self._store.session_delete(row.id)
            return None
        await self._store.session_touch(row.id, now)
        return User(row.username)

    async def revoke(self, token: str) -> None:
        await self._store.session_delete(token_hash(token))


class NodeTokens:
    def __init__(self, store: AsyncStore):
        self._store = store

    async def create(self, name: str) -> tuple[str, str]:
        token = new_token(NODE_TOKEN_PREFIX)
        id_ = secrets.token_hex(8)
        await self._store.node_token_create(id_, name, token_hash(token), time.time())
        return id_, token

    async def resolve(self, token: str) -> Node | None:
        row = await self._store.node_token_by_hash(token_hash(token))
        if row is None:
            return None
        await self._store.node_token_touch(row.id, time.time())
        return Node(row.id, row.name)

    async def revoke(self, id_: str) -> bool:
        return await self._store.node_token_revoke(id_, time.time())

    async def list(self) -> list[NodeTokenRow]:
        return await self._store.node_tokens_list()


class LoginLimiter:
    """Per-IP lockout after repeated failures. In memory: a restart forgives."""

    def __init__(self, max_failures: int = 5, lockout_s: float = 60.0,
                 clock: Callable[[], float] = time.time):
        self._max = max_failures
        self._lockout_s = lockout_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str) -> int:
        until = self._locked_until.get(ip, 0.0)
        remaining = until - self._clock()
        if remaining <= 0:
            self._locked_until.pop(ip, None)
            return 0
        return int(remaining + 0.999)

    def allowed(self, ip: str) -> bool:
        return self.retry_after(ip) == 0

    def record_failure(self, ip: str) -> None:
        count = self._failures.get(ip, 0) + 1
        self._failures[ip] = count
        if count >= self._max:
            self._locked_until[ip] = self._clock() + self._lockout_s
            self._failures[ip] = 0

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)


# --------------------------------------------------------------- requests

def client_ip(request, trusted_proxy: bool) -> str:
    if trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def is_https(request, trusted_proxy: bool) -> bool:
    if getattr(request, "secure", False):
        return True
    return trusted_proxy and request.headers.get("X-Forwarded-Proto", "").lower() == "https"
