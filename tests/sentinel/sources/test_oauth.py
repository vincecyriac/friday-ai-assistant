import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, SourceError
from friday.sentinel.sources.oauth import SCOPES, GoogleOAuth
from tests.sentinel.sources.conftest import fixed_clock


async def _oauth(server, clock=None):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh-1",
                        token_url=f"{server.url}/token",
                        clock=clock.read if clock else (lambda: 0.0))
    return session, oauth


async def test_scopes_are_read_and_compose_only():
    assert SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/gmail.compose",
                      "https://www.googleapis.com/auth/calendar.events")
    assert not any("gmail.send" in s or s.endswith("/gmail.modify") for s in SCOPES)


async def test_token_is_fetched_once_and_cached(google_server):
    clock = fixed_clock()
    session, oauth = await _oauth(google_server, clock)
    try:
        assert await oauth.token() == "tok-1"
        assert await oauth.token() == "tok-1"
        assert len(google_server.calls) == 1              # cached, not refetched
        body = google_server.calls[0]["body"]
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "refresh-1"
        assert body["client_id"] == "cid" and body["client_secret"] == "secret"
    finally:
        await session.close()


async def test_token_is_refreshed_before_it_expires(google_server):
    clock = fixed_clock()
    google_server.token_responses = [{"access_token": "tok-1", "expires_in": 3600},
                                     {"access_token": "tok-2", "expires_in": 3600}]
    session, oauth = await _oauth(google_server, clock)
    try:
        assert await oauth.token() == "tok-1"
        clock.tick(3550)          # past 3600-60: inside the refresh margin
        assert await oauth.token() == "tok-2"
        clock.tick(1)
        assert await oauth.token() == "tok-2"          # the new token is cached in turn
        assert len(google_server.calls) == 2
    finally:
        await session.close()


async def test_invalid_grant_is_auth_expired_not_a_retry(google_server):
    google_server.status["token"] = 400
    session, oauth = await _oauth(google_server)
    try:
        with pytest.raises(AuthExpired):
            await oauth.token()
    finally:
        await session.close()


async def test_a_server_error_is_a_transient_source_error(google_server):
    google_server.status["token"] = 503
    session, oauth = await _oauth(google_server)
    try:
        with pytest.raises(SourceError) as excinfo:
            await oauth.token()
        assert not isinstance(excinfo.value, AuthExpired)
        assert "secret" not in str(excinfo.value) and "refresh-1" not in str(excinfo.value)
    finally:
        await session.close()


async def test_credentials_key_changes_when_the_token_does(google_server):
    session, oauth = await _oauth(google_server)
    try:
        other = GoogleOAuth(session, "cid", "secret", "refresh-2",
                            token_url=f"{google_server.url}/token")
        assert oauth.credentials_key != other.credentials_key
    finally:
        await session.close()
