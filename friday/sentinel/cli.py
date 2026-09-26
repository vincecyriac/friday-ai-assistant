"""``friday-sentinel`` command line: run the daemon, or bootstrap it.

Bootstrap commands open the SQLite store directly. WAL and busy_timeout make
that safe while the daemon is running; every change is audited as
``actor="cli"``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import http.server
import json
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

from friday.core.config import ConfigError, Settings, load_settings
from friday.core.storage import Store
from friday.core.vault import Vault
from friday.sentinel.auth import NODE_TOKEN_PREFIX, hash_password, new_token, token_hash
from friday.sentinel.sources.oauth import SCOPES, TOKEN_URL


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="friday-sentinel", description="FRIDAY sentinel daemon and bootstrap tools")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start the daemon (default)")
    sub.add_parser("keygen", help="print a new FRIDAY_MASTER_KEY line for .env")
    user = sub.add_parser("user", help="dashboard user management").add_subparsers(dest="user_command")
    sp = user.add_parser("set-password", help="create the user or change its password")
    sp.add_argument("name")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    token = sub.add_parser("token", help="node token management").add_subparsers(dest="token_command")
    token.add_parser("create", help="mint a node token (printed once)").add_argument("name")
    token.add_parser("list", help="list node tokens")
    token.add_parser("revoke", help="revoke a node token").add_argument("id")
    google = sub.add_parser("google-auth", help="grant Gmail and Calendar access (one time)")
    google.add_argument("--print-only", action="store_true",
                        help="print the refresh token instead of storing it")
    google.add_argument("--revoke", action="store_true", help="forget the stored refresh token")
    return p


def open_store(settings: Settings) -> Store:
    Vault.from_master_key(settings.master_key or "")      # fail early with the keygen hint
    return Store.open(settings.data_dir / "sentinel.db", synchronous=settings.db_synchronous)


def _cmd_run() -> int:
    from friday.sentinel.daemon import Sentinel
    return asyncio.run(Sentinel(load_settings()).run())


def _cmd_keygen() -> int:
    # When piped (keygen >> .env) start on a fresh line: a .env whose last line
    # has no trailing newline would otherwise swallow the key into that line.
    prefix = "" if sys.stdout.isatty() else "\n"
    print(f"{prefix}FRIDAY_MASTER_KEY={Vault.generate_master_key()}")
    return 0


def _cmd_set_password(name: str, from_stdin: bool) -> int:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"Password for {name}: ")
        if password != getpass.getpass("Repeat password: "):
            print("friday-sentinel: passwords do not match", file=sys.stderr)
            return 1
    if not password:
        print("friday-sentinel: password must not be empty", file=sys.stderr)
        return 1
    store = open_store(load_settings())
    try:
        now = time.time()
        store.user_upsert(name, hash_password(password), now)
        store.audit_append(now, "cli", "user.set_password", name, {})
    finally:
        store.close()
    print(f"password set for user {name}")
    return 0


def _cmd_token_create(name: str) -> int:
    store = open_store(load_settings())
    try:
        token = new_token(NODE_TOKEN_PREFIX)
        id_ = secrets.token_hex(8)
        now = time.time()
        store.node_token_create(id_, name, token_hash(token), now)
        store.audit_append(now, "cli", "token.create", id_, {"name": name})
    finally:
        store.close()
    print(f"node token {id_} ({name}) created. Shown once — store it as FRIDAY_SENTINEL_TOKEN on the node:")
    print(token)
    return 0


def _cmd_token_list() -> int:
    store = open_store(load_settings())
    try:
        rows = store.node_tokens_list()
    finally:
        store.close()
    if not rows:
        print("no node tokens")
        return 0
    print(f"{'id':18} {'name':20} {'created':20} {'last used':20} status")
    for r in rows:
        status = "revoked" if r.revoked_at else "active"
        print(f"{r.id:18} {r.name:20} {_when(r.created_at):20} {_when(r.last_used):20} {status}")
    return 0


def _cmd_token_revoke(id_: str) -> int:
    store = open_store(load_settings())
    try:
        ok = store.node_token_revoke(id_, time.time())
        if ok:
            store.audit_append(time.time(), "cli", "token.revoke", id_, {})
    finally:
        store.close()
    if not ok:
        print(f"friday-sentinel: no active token with id {id_}", file=sys.stderr)
        return 1
    print(f"token {id_} revoked")
    return 0


def _when(ts: float | None) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_PORT = 8823
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}"


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str, *,
                  token_url: str = TOKEN_URL, urlopen=urllib.request.urlopen) -> str:
    payload = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    request = urllib.request.Request(token_url, data=payload,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    token = body.get("refresh_token")
    if not token:
        raise RuntimeError(
            "Google returned no refresh token. This happens when the account has already "
            "granted access: revoke it at https://myaccount.google.com/permissions and retry.")
    return token


def _consent_flow(client_id: str, client_secret: str) -> str:
    """Serve one redirect on the loopback, then trade the code for a refresh token."""
    state = secrets.token_urlsafe(16)
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802 (stdlib naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            ok = captured.get("state") == state and "code" in captured
            self.wfile.write(b"FRIDAY: you can close this tab." if ok
                             else b"FRIDAY: consent failed; check the terminal.")

        def log_message(self, *args):                       # keep the terminal clean
            return

    print("Open this URL, grant access, and the browser will redirect back here:")
    print(f"  {build_auth_url(client_id, REDIRECT_URI, state)}")
    print("Waiting for the redirect…")
    with http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler) as server:
        server.timeout = 300
        server.handle_request()
    if captured.get("state") != state:
        raise RuntimeError("the redirect did not carry the expected state; nothing was stored")
    if "code" not in captured:
        raise RuntimeError(f"Google reported: {captured.get('error', 'no code returned')}")
    return exchange_code(captured["code"], client_id, client_secret, REDIRECT_URI)


def _cmd_google_auth(print_only: bool, revoke: bool) -> int:
    from friday.core.vault import Vault

    settings = load_settings()
    store = open_store(settings)
    vault = Vault.from_master_key(settings.master_key or "")
    try:
        if revoke:
            store.setting_delete("sources.google.refresh_token")
            store.audit_append(time.time(), "cli", "sources.google.consent", None,
                               {"action": "revoked"})
            print("Google refresh token revoked. Gmail and Calendar will report needs_reauth.")
            return 0

        client_id = _plain_setting(store, "sources.google.client_id")
        secret_row = store.setting_get("sources.google.client_secret")
        client_secret = vault.decrypt("sources.google.client_secret", secret_row.value) \
            if secret_row else ""
        if not client_id or not client_secret:
            print("friday-sentinel: set sources.google.client_id and sources.google.client_secret "
                  "in the dashboard (Settings → sources) before running consent.", file=sys.stderr)
            return 1

        token = _consent_flow(client_id, client_secret)
        if print_only:
            print("Paste this into Settings → sources on the sentinel:")
            print(f"  sources.google.refresh_token = {token}")
            return 0
        now = time.time()
        store.setting_set("sources.google.refresh_token",
                          vault.encrypt("sources.google.refresh_token", token),
                          secret=True, updated_by="cli", ts=now)
        store.audit_append(now, "cli", "sources.google.consent", None,
                           {"action": "stored", "scopes": list(SCOPES)})
        print("Refresh token stored in the vault. Gmail and Calendar are ready.")
        return 0
    finally:
        store.close()


def _plain_setting(store, key: str) -> str:
    row = store.setting_get(key)
    if row is None:
        return ""
    try:
        return str(json.loads(row.value))
    except ValueError:
        return str(row.value)

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in (None, "run"):
            return _cmd_run()
        if args.command == "keygen":
            return _cmd_keygen()
        if args.command == "user" and args.user_command == "set-password":
            return _cmd_set_password(args.name, args.password_stdin)
        if args.command == "token":
            if args.token_command == "create":
                return _cmd_token_create(args.name)
            if args.token_command == "list":
                return _cmd_token_list()
            if args.token_command == "revoke":
                return _cmd_token_revoke(args.id)
        if args.command == "google-auth":
            return _cmd_google_auth(args.print_only, args.revoke)
        _parser().print_help()
        return 2
    except ConfigError as e:
        print(f"friday-sentinel: configuration error: {e}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as e:
        print(f"friday-sentinel: cannot start: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
