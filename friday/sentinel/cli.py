"""``friday-sentinel`` command line: run the daemon, or bootstrap it.

Bootstrap commands open the SQLite store directly. WAL and busy_timeout make
that safe while the daemon is running; every change is audited as
``actor="cli"``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sqlite3
import sys
import time

from friday.core.config import ConfigError, Settings, load_settings
from friday.core.storage import Store
from friday.core.vault import Vault
from friday.sentinel.auth import NODE_TOKEN_PREFIX, hash_password, new_token, token_hash


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
