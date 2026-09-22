import re
import time

import pytest

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from friday.sentinel.web import DASHBOARD_DIR


def test_dashboard_files_exist_and_use_relative_urls():
    assert (DASHBOARD_DIR / "index.html").is_file()
    html = (DASHBOARD_DIR / "index.html").read_text()
    assert 'src="static/app.js"' in html and 'href="static/style.css"' in html
    js = (DASHBOARD_DIR / "app.js").read_text()
    assert "X-FRIDAY-Client" in js
    assert not re.search(r"""["']/(api|auth|static|config)""", js), "absolute URLs break reverse-proxy prefixes"


async def test_real_dashboard_is_served(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    client = await aiohttp_client(create_app(services))
    resp = await client.get("/login")
    assert resp.status == 200 and 'id="app"' in await resp.text()
    assert (await client.get("/static/style.css")).status == 200
    assert (await client.get("/static/app.js")).status == 200
    assert (await client.get("/", allow_redirects=False)).status == 302
