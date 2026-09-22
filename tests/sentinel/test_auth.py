import time
from types import SimpleNamespace

import pytest

from friday.core.storage import AsyncStore
from friday.sentinel.auth import (NODE_TOKEN_PREFIX, LoginLimiter, Node, NodeTokens, SessionManager, User,
                                  client_ip, hash_password, is_https, new_token, token_hash, verify_password)


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$32768$8$1$")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong", stored) is False
    assert hash_password("correct horse") != stored                  # fresh salt


def test_verify_tolerates_garbage():
    assert verify_password("x", "") is False
    assert verify_password("x", "bcrypt$nope") is False
    assert verify_password("x", "scrypt$a$b$c$d$e") is False


def test_tokens():
    t = new_token("fn_")
    assert t.startswith("fn_") and len(t) > 40 and new_token("fn_") != t
    assert token_hash(t) == token_hash(t) and len(token_hash(t)) == 64
    assert NODE_TOKEN_PREFIX == "fn_"


@pytest.fixture
async def store(tmp_path):
    s = await AsyncStore.open(tmp_path / "t.db")
    yield s
    await s.aclose()


async def test_sessions(store):
    sessions = SessionManager(store, ttl_s=100.0)
    token = await sessions.create("vince", user_agent="ua", ip="1.1.1.1")
    assert await sessions.resolve(token) == User("vince")
    assert await sessions.resolve("nope") is None
    assert (await store.session_get(token_hash(token))).user_agent == "ua"
    await sessions.revoke(token)
    assert await sessions.resolve(token) is None
    await sessions.revoke(token)                                         # idempotent


async def test_session_expiry(store):
    sessions = SessionManager(store, ttl_s=0.01)
    token = await sessions.create("vince", user_agent=None, ip=None)
    time.sleep(0.02)
    assert await sessions.resolve(token) is None
    assert await store.session_get(token_hash(token)) is None            # expired row removed on touch


async def test_node_tokens(store):
    tokens = NodeTokens(store)
    id_, plaintext = await tokens.create("desktop")
    assert plaintext.startswith("fn_")
    assert await tokens.resolve(plaintext) == Node(id_, "desktop")
    assert await tokens.resolve("fn_nope") is None
    listed = await tokens.list()
    assert [t.name for t in listed] == ["desktop"] and listed[0].last_used is not None
    assert await tokens.revoke(id_) is True
    assert await tokens.resolve(plaintext) is None
    assert await tokens.revoke(id_) is False


def test_login_limiter(monkeypatch):
    now = [1000.0]
    limiter = LoginLimiter(max_failures=3, lockout_s=60.0, clock=lambda: now[0])
    ip = "9.9.9.9"
    assert limiter.allowed(ip)
    for _ in range(3):
        limiter.record_failure(ip)
    assert not limiter.allowed(ip) and limiter.retry_after(ip) == 60
    now[0] += 30
    assert limiter.retry_after(ip) == 30
    now[0] += 31
    assert limiter.allowed(ip)
    limiter.record_failure(ip)
    limiter.reset(ip)
    assert limiter.allowed(ip) and limiter.retry_after(ip) == 0
    assert limiter.allowed("other")


def _request(headers=None, remote="10.0.0.5", secure=False):
    return SimpleNamespace(headers=headers or {}, remote=remote, secure=secure)


def test_client_ip_and_https():
    r = _request({"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "X-Forwarded-Proto": "https"})
    assert client_ip(r, trusted_proxy=False) == "10.0.0.5"
    assert client_ip(r, trusted_proxy=True) == "203.0.113.9"
    assert is_https(r, trusted_proxy=False) is False
    assert is_https(r, trusted_proxy=True) is True
    assert is_https(_request(secure=True), trusted_proxy=False) is True
    assert client_ip(_request(remote=None), trusted_proxy=False) == "unknown"
