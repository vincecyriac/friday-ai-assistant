import json
import urllib.parse
from io import BytesIO

import pytest

from friday.core.storage import Store
from friday.sentinel import cli
from friday.sentinel.sources.oauth import SCOPES
from tests.conftest import TEST_MASTER_KEY


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    return tmp_path / "data"


def _seed_client(env, client_id="cid", secret="shh"):
    from friday.core.config import load_settings
    from friday.core.vault import Vault
    settings = load_settings()
    vault = Vault.from_master_key(settings.master_key)
    store = Store.open(env / "sentinel.db")
    try:
        store.setting_set("sources.google.client_id", json.dumps(client_id), secret=False,
                          updated_by="test", ts=1.0)
        store.setting_set("sources.google.client_secret", vault.encrypt(
            "sources.google.client_secret", secret), secret=True, updated_by="test", ts=1.0)
    finally:
        store.close()


def test_auth_url_asks_for_offline_access_and_only_our_scopes():
    url = cli.build_auth_url("cid", "http://127.0.0.1:8823", "state-1")
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["cid"] and query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"] and query["state"] == ["state-1"]
    assert query["scope"][0].split() == list(SCOPES)
    assert "gmail.send" not in query["scope"][0] and "gmail.modify" not in query["scope"][0]


def test_exchange_code_returns_the_refresh_token():
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = dict(urllib.parse.parse_qsl(request.data.decode()))
        return BytesIO(json.dumps({"refresh_token": "refresh-xyz",
                                   "access_token": "a", "expires_in": 3600}).encode())

    token = cli.exchange_code("the-code", "cid", "shh", "http://127.0.0.1:8823",
                              urlopen=fake_urlopen)
    assert token == "refresh-xyz"
    assert captured["body"]["grant_type"] == "authorization_code"
    assert captured["body"]["code"] == "the-code"


def test_exchange_code_explains_a_missing_refresh_token():
    def fake_urlopen(request, timeout=0):
        return BytesIO(json.dumps({"access_token": "a"}).encode())

    with pytest.raises(RuntimeError) as excinfo:
        cli.exchange_code("c", "cid", "shh", "http://x", urlopen=fake_urlopen)
    assert "refresh token" in str(excinfo.value).lower()


def test_google_auth_requires_the_client_credentials_first(env, capsys):
    assert cli.main(["google-auth", "--print-only"]) == 1
    assert "sources.google.client_id" in capsys.readouterr().err


def test_google_auth_print_only_does_not_touch_the_vault(env, monkeypatch, capsys):
    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-printed")
    assert cli.main(["google-auth", "--print-only"]) == 0
    out = capsys.readouterr().out
    assert "refresh-printed" in out and "sources.google.refresh_token" in out
    store = Store.open(env / "sentinel.db")
    try:
        assert store.setting_get("sources.google.refresh_token") is None
    finally:
        store.close()


def test_google_auth_stores_the_token_encrypted_and_audits_without_it(env, monkeypatch, capsys):
    from friday.core.config import load_settings
    from friday.core.vault import Vault

    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-stored")
    assert cli.main(["google-auth"]) == 0
    assert "refresh-stored" not in capsys.readouterr().out        # never echoed when stored

    vault = Vault.from_master_key(load_settings().master_key)
    store = Store.open(env / "sentinel.db")
    try:
        row = store.setting_get("sources.google.refresh_token")
        assert row.secret is True and row.value.startswith("v1:")
        assert vault.decrypt("sources.google.refresh_token", row.value) == "refresh-stored"
        entry = [a for a in store.audit_list() if a.action == "sources.google.consent"][0]
        assert "refresh-stored" not in json.dumps(entry.detail)
    finally:
        store.close()


def test_revoke_clears_the_token(env, monkeypatch, capsys):
    _seed_client(env)
    monkeypatch.setattr(cli, "_consent_flow", lambda client_id, secret: "refresh-stored")
    cli.main(["google-auth"])
    assert cli.main(["google-auth", "--revoke"]) == 0
    store = Store.open(env / "sentinel.db")
    try:
        assert store.setting_get("sources.google.refresh_token") is None
    finally:
        store.close()
    assert "revoked" in capsys.readouterr().out.lower()
