import time
from types import SimpleNamespace

import pytest

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from friday.sentinel.sources import WatchItem
from tests.sentinel.conftest import node_headers

CSRF = {"X-FRIDAY-Client": "dashboard"}


@pytest.fixture
async def client(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    c = await aiohttp_client(create_app(services))
    assert (await c.post("/auth/login", json={"username": "vince", "password": "pw"},
                         headers=CSRF)).status == 204
    return c


def _status():
    return {"email": {"state": "watching", "enabled": True, "configured": True,
                      "last_poll": 1000.0, "last_error": "", "items_today": 4},
            "jira": {"state": "needs_reauth", "enabled": True, "configured": True,
                     "last_poll": 900.0, "last_error": "jira refused the API token",
                     "items_today": 0}}


async def test_watch_reports_sources_and_recent_items(client, services):
    services.watch = SimpleNamespace(status=_status)
    await services.store.watch_item_add(
        WatchItem(source="email", external_id="gmail:1", title="Status update", snippet="body",
                  who="Ada", url="https://mail/1", ts=50.0, meta={"account": "gmail"}), 60.0)

    body = await (await client.get("/api/watch")).json()
    assert body["sources"]["email"]["state"] == "watching"
    assert body["sources"]["jira"]["last_error"] == "jira refused the API token"
    assert len(body["recent"]) == 1
    row = body["recent"][0]
    assert row["id"] == "email:gmail:1" and row["title"] == "Status update"
    assert row["who"] == "Ada" and row["url"] == "https://mail/1" and row["first_seen"] == 60.0


async def test_watch_is_empty_before_the_runner_exists(client, services):
    services.watch = None
    body = await (await client.get("/api/watch")).json()
    assert body == {"sources": {}, "recent": []}


async def test_watch_never_leaks_a_snippet_body_to_the_list(client, services):
    """The card shows titles, not message bodies — a shoulder-surfer sees less."""
    services.watch = SimpleNamespace(status=_status)
    await services.store.watch_item_add(
        WatchItem(source="email", external_id="gmail:2", title="Subject",
                  snippet="salary details inside", who="HR", url="", ts=1.0, meta={}), 2.0)
    body = await (await client.get("/api/watch")).json()
    assert "salary" not in str(body)


async def test_watch_requires_a_session(aiohttp_client, services, node_token):
    anon = await aiohttp_client(create_app(services))
    assert (await anon.get("/api/watch")).status == 401
    assert (await anon.get("/api/watch", headers=node_headers(node_token))).status == 401
