import base64
import io

import pytest

from friday.core.storage import Store
from friday.sentinel import cli
from friday.sentinel.auth import token_hash, verify_password
from tests.conftest import TEST_MASTER_KEY


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setenv("FRIDAY_SENTINEL_BIND", "127.0.0.1:0")
    return tmp_path / "data"


def test_keygen_prints_env_line(capsys):
    assert cli.main(["keygen"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("FRIDAY_MASTER_KEY=")
    assert len(base64.urlsafe_b64decode(line.split("=", 1)[1])) == 32


def test_keygen_appends_safely_to_env_without_trailing_newline(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TRIPO_API_KEY=tripo-value")                  # no trailing newline
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "friday.sentinel", "keygen"], capture_output=True, text=True, check=True).stdout
    with env_file.open("a") as f:
        f.write(out)
    from dotenv import dotenv_values
    values = dotenv_values(env_file)
    assert values["TRIPO_API_KEY"] == "tripo-value"
    assert len(base64.urlsafe_b64decode(values["FRIDAY_MASTER_KEY"])) == 32


def test_set_password_from_stdin(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    assert cli.main(["user", "set-password", "vince", "--password-stdin"]) == 0
    assert "vince" in capsys.readouterr().out
    store = Store.open(env / "sentinel.db")
    try:
        assert verify_password("hunter2", store.user_get("vince").password_hash)
        assert store.audit_list()[0].action == "user.set_password"
    finally:
        store.close()


def test_set_password_rejects_empty(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["user", "set-password", "vince", "--password-stdin"]) == 1
    assert "empty" in capsys.readouterr().err


def test_token_lifecycle(env, capsys):
    assert cli.main(["token", "create", "desktop"]) == 0
    out = capsys.readouterr().out
    token = next(word for word in out.split() if word.startswith("fn_"))
    store = Store.open(env / "sentinel.db")
    try:
        row = store.node_token_by_hash(token_hash(token))
        assert row.name == "desktop"
    finally:
        store.close()
    assert cli.main(["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "desktop" in listing and row.id in listing and token not in listing
    assert cli.main(["token", "revoke", row.id]) == 0
    assert cli.main(["token", "revoke", row.id]) == 1
    store = Store.open(env / "sentinel.db")
    try:
        assert store.node_token_by_hash(token_hash(token)) is None
    finally:
        store.close()


def test_store_commands_need_master_key(env, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_MASTER_KEY", "")        # empty beats any value in the developer's real .env
    assert cli.main(["token", "list"]) == 1
    assert "FRIDAY_MASTER_KEY" in capsys.readouterr().err


def test_main_module_delegates():
    from friday.sentinel.__main__ import main
    assert main is cli.main
