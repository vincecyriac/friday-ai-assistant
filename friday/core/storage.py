"""Embedded SQLite persistence: state cache, node heartbeats, the durable
event queue and a telemetry log.

Power-loss posture: WAL journal (a torn write can only ever affect the WAL,
never the main file) plus ``synchronous=FULL`` by default (every commit is
fsynced, so a committed event survives a cut). ``NORMAL`` is an opt-in for
SD-card installs: still corruption-safe, may lose the last few commits.

``Store`` is synchronous and single-connection. ``AsyncStore`` serialises
every call onto one dedicated thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from friday.core.config import ConfigError
from friday.core.events import Event

log = logging.getLogger(__name__)

MIN_SQLITE = (3, 35, 0)                     # UPDATE ... RETURNING
SYNC_MODES = ("FULL", "NORMAL")
CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
EVENT_STATUSES = ("pending", "processing", "done", "failed")

# Forward-only migrations. Each entry is a tuple of single statements (never
# executescript(): it issues an implicit COMMIT first) applied in one
# transaction together with the schema_version bump.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE kv ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)",
        "CREATE TABLE heartbeats ("
        "  node_id TEXT PRIMARY KEY, last_seen REAL NOT NULL,"
        "  status TEXT NOT NULL, meta TEXT NOT NULL)",
        "CREATE TABLE events ("
        "  id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,"
        "  type TEXT NOT NULL, payload TEXT NOT NULL,"
        "  priority INTEGER NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  created_at REAL NOT NULL, claimed_at REAL, processed_at REAL, error TEXT)",
        "CREATE INDEX events_status_ts ON events(status, priority DESC, ts)",
        "CREATE TABLE telemetry ("
        "  ts REAL NOT NULL, node_id TEXT NOT NULL, snapshot TEXT NOT NULL)",
        "CREATE INDEX telemetry_node_ts ON telemetry(node_id, ts DESC)",
    ),
    2: (
        "CREATE TABLE settings ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL DEFAULT 0,"
        "  updated_at REAL NOT NULL, updated_by TEXT NOT NULL)",
        "CREATE TABLE users ("
        "  username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,"
        "  created_at REAL NOT NULL, password_changed_at REAL NOT NULL)",
        "CREATE TABLE sessions ("
        "  id TEXT PRIMARY KEY, username TEXT NOT NULL, created_at REAL NOT NULL,"
        "  expires_at REAL NOT NULL, last_seen REAL NOT NULL, user_agent TEXT, ip TEXT)",
        "CREATE INDEX sessions_expires ON sessions(expires_at)",
        "CREATE TABLE node_tokens ("
        "  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,"
        "  created_at REAL NOT NULL, last_used REAL, revoked_at REAL)",
        "CREATE TABLE audit ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,"
        "  action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL)",
        "CREATE INDEX audit_ts ON audit(ts DESC)",
    ),
}

_EVENT_COLUMNS = "id, ts, source, type, payload, priority, status, attempts, error, claimed_at, processed_at"


@dataclass(frozen=True)
class HeartbeatRow:
    node_id: str
    last_seen: float
    status: str
    meta: dict[str, Any]


@dataclass(frozen=True)
class StoredEvent:
    event: Event
    status: str
    attempts: int
    error: str | None
    claimed_at: float | None
    processed_at: float | None


@dataclass(frozen=True)
class SettingRow:
    key: str
    value: str
    secret: bool
    updated_at: float
    updated_by: str


@dataclass(frozen=True)
class UserRow:
    username: str
    password_hash: str
    created_at: float
    password_changed_at: float


@dataclass(frozen=True)
class SessionRow:
    id: str
    username: str
    created_at: float
    expires_at: float
    last_seen: float
    user_agent: str | None
    ip: str | None


@dataclass(frozen=True)
class NodeTokenRow:
    id: str
    name: str
    created_at: float
    last_used: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: float
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]


def _row_to_stored(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        event=Event(id=row["id"], ts=row["ts"], source=row["source"], type=row["type"],
                    payload=json.loads(row["payload"]), priority=row["priority"]),
        status=row["status"], attempts=row["attempts"], error=row["error"],
        claimed_at=row["claimed_at"], processed_at=row["processed_at"],
    )


def _quarantine(path: Path) -> None:
    stamp = int(time.time())
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{path}{suffix}")
        if src.exists():
            dst = Path(f"{path}.corrupt-{stamp}{suffix}")
            src.rename(dst)
            log.error("quarantined corrupt database file %s -> %s", src, dst)


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self._conn = conn
        self.path = path

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, path: Path, *, synchronous: str = "FULL") -> "Store":
        synchronous = str(synchronous).upper()
        if synchronous not in SYNC_MODES:
            raise ConfigError(
                f"FRIDAY_DB_SYNCHRONOUS must be one of {SYNC_MODES}, got {synchronous!r}")
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise ConfigError(
                f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ is required (RETURNING support); "
                f"this Python links {sqlite3.sqlite_version}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn: sqlite3.Connection | None = None
        healthy = False
        try:
            conn = cls._connect(path, synchronous)
            healthy = cls._quick_check(conn)
        except sqlite3.DatabaseError as e:
            log.error("database %s is unreadable: %s", path, e)
        if not healthy:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            _quarantine(path)
            conn = cls._connect(path, synchronous)

        store = cls(conn, path)
        store._migrate()
        return store

    @staticmethod
    def _connect(path: Path, synchronous: str) -> sqlite3.Connection:
        # isolation_level=None: autocommit; transactions are explicit BEGIN/COMMIT.
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={synchronous}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    @staticmethod
    def _quick_check(conn: sqlite3.Connection) -> bool:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return row is not None and row[0] == "ok"

    def _migrate(self) -> None:
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = c.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            c.execute("INSERT INTO schema_version (version) VALUES (0)")
            current = 0
        else:
            current = int(row[0])
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            c.execute("BEGIN")
            try:
                for statement in MIGRATIONS[version]:
                    c.execute(statement)
                c.execute("UPDATE schema_version SET version = ?", (version,))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            log.info("database schema migrated to v%d", version)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def schema_version(self) -> int:
        return int(self._conn.execute("SELECT version FROM schema_version").fetchone()[0])

    def pragma(self, name: str) -> Any:
        return self._conn.execute(f"PRAGMA {name}").fetchone()[0]

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        mode = mode.upper()
        if mode not in CHECKPOINT_MODES:
            raise ValueError(f"checkpoint mode must be one of {CHECKPOINT_MODES}")
        self._conn.execute(f"PRAGMA wal_checkpoint({mode})")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------- kv

    def kv_get(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def kv_set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), time.time()))

    def kv_delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ------------------------------------------------------------ heartbeats

    def heartbeat_upsert(self, node_id: str, status: str, meta: dict, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO heartbeats (node_id, last_seen, status, meta) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen, "
            "status = excluded.status, meta = excluded.meta",
            (node_id, ts, status, json.dumps(meta)))

    def heartbeats(self) -> list[HeartbeatRow]:
        rows = self._conn.execute(
            "SELECT node_id, last_seen, status, meta FROM heartbeats ORDER BY node_id").fetchall()
        return [HeartbeatRow(r["node_id"], r["last_seen"], r["status"], json.loads(r["meta"]))
                for r in rows]

    # ----------------------------------------------------------- event queue

    def enqueue(self, event: Event) -> None:
        event.validate()
        self._conn.execute(
            "INSERT INTO events (id, ts, source, type, payload, priority, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (event.id, event.ts, event.source, event.type,
             json.dumps(dict(event.payload)), event.priority, time.time()))

    def claim(self, limit: int, now: float) -> list[StoredEvent]:
        """Atomically move up to ``limit`` pending events to processing."""
        rows = self._conn.execute(
            "UPDATE events SET status = 'processing', claimed_at = ?, attempts = attempts + 1 "
            "WHERE id IN (SELECT id FROM events WHERE status = 'pending' "
            "             ORDER BY priority DESC, ts LIMIT ?) "
            f"RETURNING {_EVENT_COLUMNS}",
            (now, limit)).fetchall()
        items = [_row_to_stored(r) for r in rows]
        items.sort(key=lambda s: (-s.event.priority, s.event.ts))   # RETURNING order is unspecified
        return items

    def complete(self, event_id: str, now: float) -> None:
        self._conn.execute(
            "UPDATE events SET status = 'done', processed_at = ?, error = NULL WHERE id = ?",
            (now, event_id))

    def fail(self, event_id: str, error: str, now: float, *, retry: bool) -> None:
        if retry:
            self._conn.execute(
                "UPDATE events SET status = 'pending', error = ?, claimed_at = NULL WHERE id = ?",
                (error, event_id))
        else:
            self._conn.execute(
                "UPDATE events SET status = 'failed', error = ?, processed_at = ? WHERE id = ?",
                (error, now, event_id))

    def requeue_stale(self, older_than_s: float, now: float) -> int:
        """Return processing rows claimed at or before ``now - older_than_s`` to pending."""
        cur = self._conn.execute(
            "UPDATE events SET status = 'pending', claimed_at = NULL "
            "WHERE status = 'processing' AND (claimed_at IS NULL OR claimed_at <= ?)",
            (now - older_than_s,))
        return cur.rowcount

    def queue_depths(self) -> dict[str, int]:
        depths = {status: 0 for status in EVENT_STATUSES}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status"):
            depths[row["status"]] = row["n"]
        return depths

    def list_events(self, *, type: str | None = None, status: str | None = None,
                    limit: int = 100) -> list[StoredEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit)).fetchall()
        return [_row_to_stored(r) for r in rows]

    # ------------------------------------------------------------- telemetry

    def telemetry_insert(self, node_id: str, snapshot: dict, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO telemetry (ts, node_id, snapshot) VALUES (?, ?, ?)",
            (ts, node_id, json.dumps(snapshot)))

    def telemetry_latest(self, node_id: str | None = None) -> dict | None:
        if node_id is None:
            row = self._conn.execute(
                "SELECT snapshot FROM telemetry ORDER BY ts DESC LIMIT 1").fetchone()
        else:
            row = self._conn.execute(
                "SELECT snapshot FROM telemetry WHERE node_id = ? ORDER BY ts DESC LIMIT 1",
                (node_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def telemetry_count(self, node_id: str | None = None) -> int:
        if node_id is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0])
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE node_id = ?", (node_id,)).fetchone()[0])

    def prune(self, older_than_ts: float) -> dict[str, int]:
        """Drop old telemetry and finished events. Pending/processing are never touched."""
        telemetry = self._conn.execute(
            "DELETE FROM telemetry WHERE ts < ?", (older_than_ts,)).rowcount
        events = self._conn.execute(
            "DELETE FROM events WHERE status IN ('done', 'failed') AND created_at < ?",
            (older_than_ts,)).rowcount
        return {"telemetry": telemetry, "events": events}

    # -------------------------------------------------------------- settings

    def setting_get(self, key: str) -> SettingRow | None:
        row = self._conn.execute(
            "SELECT key, value, secret, updated_at, updated_by FROM settings WHERE key = ?",
            (key,)).fetchone()
        return None if row is None else SettingRow(
            row["key"], row["value"], bool(row["secret"]), row["updated_at"], row["updated_by"])

    def setting_set(self, key: str, value: str, *, secret: bool, updated_by: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value, secret, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, secret = excluded.secret, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, int(secret), ts, updated_by))

    def setting_delete(self, key: str) -> bool:
        return self._conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0

    def settings_all(self) -> list[SettingRow]:
        rows = self._conn.execute(
            "SELECT key, value, secret, updated_at, updated_by FROM settings ORDER BY key").fetchall()
        return [SettingRow(r["key"], r["value"], bool(r["secret"]), r["updated_at"], r["updated_by"])
                for r in rows]

    # ----------------------------------------------------------------- users

    def user_get(self, username: str) -> UserRow | None:
        row = self._conn.execute(
            "SELECT username, password_hash, created_at, password_changed_at FROM users WHERE username = ?",
            (username,)).fetchone()
        return None if row is None else UserRow(
            row["username"], row["password_hash"], row["created_at"], row["password_changed_at"])

    def user_upsert(self, username: str, password_hash: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO users (username, password_hash, created_at, password_changed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
            "password_hash = excluded.password_hash, password_changed_at = excluded.password_changed_at",
            (username, password_hash, ts, ts))

    def users_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # -------------------------------------------------------------- sessions

    def session_create(self, id: str, username: str, ts: float, expires_at: float,
                       user_agent: str | None, ip: str | None) -> None:
        self._conn.execute(
            "INSERT INTO sessions (id, username, created_at, expires_at, last_seen, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, username, ts, expires_at, ts, user_agent, ip))

    def session_get(self, id: str) -> SessionRow | None:
        row = self._conn.execute(
            "SELECT id, username, created_at, expires_at, last_seen, user_agent, ip "
            "FROM sessions WHERE id = ?", (id,)).fetchone()
        return None if row is None else SessionRow(
            row["id"], row["username"], row["created_at"], row["expires_at"],
            row["last_seen"], row["user_agent"], row["ip"])

    def session_touch(self, id: str, ts: float) -> None:
        self._conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (ts, id))

    def session_delete(self, id: str) -> bool:
        return self._conn.execute("DELETE FROM sessions WHERE id = ?", (id,)).rowcount > 0

    def sessions_prune(self, now: float) -> int:
        return self._conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount

    # ----------------------------------------------------------- node tokens

    def node_token_create(self, id: str, name: str, token_hash: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO node_tokens (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (id, name, token_hash, ts))

    def node_token_by_hash(self, token_hash: str) -> NodeTokenRow | None:
        row = self._conn.execute(
            "SELECT id, name, created_at, last_used, revoked_at FROM node_tokens "
            "WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)).fetchone()
        return None if row is None else NodeTokenRow(
            row["id"], row["name"], row["created_at"], row["last_used"], row["revoked_at"])

    def node_token_touch(self, id: str, ts: float) -> None:
        self._conn.execute("UPDATE node_tokens SET last_used = ? WHERE id = ?", (ts, id))

    def node_tokens_list(self) -> list[NodeTokenRow]:
        rows = self._conn.execute(
            "SELECT id, name, created_at, last_used, revoked_at FROM node_tokens ORDER BY created_at").fetchall()
        return [NodeTokenRow(r["id"], r["name"], r["created_at"], r["last_used"], r["revoked_at"])
                for r in rows]

    def node_token_revoke(self, id: str, ts: float) -> bool:
        return self._conn.execute(
            "UPDATE node_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (ts, id)).rowcount > 0

    # ----------------------------------------------------------------- audit

    def audit_append(self, ts: float, actor: str, action: str, target: str | None,
                     detail: dict[str, Any]) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (ts, actor, action, target, json.dumps(detail)))
        return int(cur.lastrowid)

    def audit_list(self, limit: int = 100, before_id: int | None = None) -> list[AuditRow]:
        if before_id is None:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, target, detail FROM audit ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, target, detail FROM audit WHERE id < ? "
                "ORDER BY id DESC LIMIT ?", (before_id, limit)).fetchall()
        return [AuditRow(r["id"], r["ts"], r["actor"], r["action"], r["target"], json.loads(r["detail"]))
                for r in rows]


class AsyncStore:
    """A ``Store`` driven from asyncio.

    All calls — including ``Store.open`` — run on one dedicated thread, so
    sqlite3's default same-thread check stays on and guards that invariant.
    """

    def __init__(self, store: Store, pool: ThreadPoolExecutor):
        self._store = store
        self._pool = pool

    @classmethod
    async def open(cls, path: Path, *, synchronous: str = "FULL") -> "AsyncStore":
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-store")
        loop = asyncio.get_running_loop()
        try:
            store = await loop.run_in_executor(
                pool, partial(Store.open, path, synchronous=synchronous))
        except BaseException:
            pool.shutdown(wait=False)
            raise
        return cls(store, pool)

    @property
    def path(self) -> Path:
        return self._store.path

    async def run(self, fn, *args, **kwargs):
        """Run any callable on the store thread and await its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))

    async def aclose(self) -> None:
        try:
            await self.run(self._store.checkpoint, "TRUNCATE")
        except Exception:
            log.exception("WAL checkpoint on close failed")
        await self.run(self._store.close)
        self._pool.shutdown(wait=True)

    # Async twins, same names and parameters as Store.
    async def checkpoint(self, mode: str = "TRUNCATE") -> None:
        await self.run(self._store.checkpoint, mode)

    async def schema_version(self) -> int:
        return await self.run(self._store.schema_version)

    async def kv_get(self, key: str) -> Any | None:
        return await self.run(self._store.kv_get, key)

    async def kv_set(self, key: str, value: Any) -> None:
        await self.run(self._store.kv_set, key, value)

    async def kv_delete(self, key: str) -> None:
        await self.run(self._store.kv_delete, key)

    async def heartbeat_upsert(self, node_id: str, status: str, meta: dict, ts: float) -> None:
        await self.run(self._store.heartbeat_upsert, node_id, status, meta, ts)

    async def heartbeats(self) -> list[HeartbeatRow]:
        return await self.run(self._store.heartbeats)

    async def enqueue(self, event: Event) -> None:
        await self.run(self._store.enqueue, event)

    async def claim(self, limit: int, now: float) -> list[StoredEvent]:
        return await self.run(self._store.claim, limit, now)

    async def complete(self, event_id: str, now: float) -> None:
        await self.run(self._store.complete, event_id, now)

    async def fail(self, event_id: str, error: str, now: float, *, retry: bool) -> None:
        await self.run(self._store.fail, event_id, error, now, retry=retry)

    async def requeue_stale(self, older_than_s: float, now: float) -> int:
        return await self.run(self._store.requeue_stale, older_than_s, now)

    async def queue_depths(self) -> dict[str, int]:
        return await self.run(self._store.queue_depths)

    async def list_events(self, *, type: str | None = None, status: str | None = None,
                          limit: int = 100) -> list[StoredEvent]:
        return await self.run(self._store.list_events, type=type, status=status, limit=limit)

    async def telemetry_insert(self, node_id: str, snapshot: dict, ts: float) -> None:
        await self.run(self._store.telemetry_insert, node_id, snapshot, ts)

    async def telemetry_latest(self, node_id: str | None = None) -> dict | None:
        return await self.run(self._store.telemetry_latest, node_id)

    async def telemetry_count(self, node_id: str | None = None) -> int:
        return await self.run(self._store.telemetry_count, node_id)

    async def prune(self, older_than_ts: float) -> dict[str, int]:
        return await self.run(self._store.prune, older_than_ts)

    async def setting_get(self, key: str) -> SettingRow | None:
        return await self.run(self._store.setting_get, key)

    async def setting_set(self, key: str, value: str, *, secret: bool, updated_by: str, ts: float) -> None:
        await self.run(self._store.setting_set, key, value, secret=secret, updated_by=updated_by, ts=ts)

    async def setting_delete(self, key: str) -> bool:
        return await self.run(self._store.setting_delete, key)

    async def settings_all(self) -> list[SettingRow]:
        return await self.run(self._store.settings_all)

    async def user_get(self, username: str) -> UserRow | None:
        return await self.run(self._store.user_get, username)

    async def user_upsert(self, username: str, password_hash: str, ts: float) -> None:
        await self.run(self._store.user_upsert, username, password_hash, ts)

    async def users_count(self) -> int:
        return await self.run(self._store.users_count)

    async def session_create(self, id: str, username: str, ts: float, expires_at: float,
                             user_agent: str | None, ip: str | None) -> None:
        await self.run(self._store.session_create, id, username, ts, expires_at, user_agent, ip)

    async def session_get(self, id: str) -> SessionRow | None:
        return await self.run(self._store.session_get, id)

    async def session_touch(self, id: str, ts: float) -> None:
        await self.run(self._store.session_touch, id, ts)

    async def session_delete(self, id: str) -> bool:
        return await self.run(self._store.session_delete, id)

    async def sessions_prune(self, now: float) -> int:
        return await self.run(self._store.sessions_prune, now)

    async def node_token_create(self, id: str, name: str, token_hash: str, ts: float) -> None:
        await self.run(self._store.node_token_create, id, name, token_hash, ts)

    async def node_token_by_hash(self, token_hash: str) -> NodeTokenRow | None:
        return await self.run(self._store.node_token_by_hash, token_hash)

    async def node_token_touch(self, id: str, ts: float) -> None:
        await self.run(self._store.node_token_touch, id, ts)

    async def node_tokens_list(self) -> list[NodeTokenRow]:
        return await self.run(self._store.node_tokens_list)

    async def node_token_revoke(self, id: str, ts: float) -> bool:
        return await self.run(self._store.node_token_revoke, id, ts)

    async def audit_append(self, ts: float, actor: str, action: str, target: str | None,
                           detail: dict[str, Any]) -> int:
        return await self.run(self._store.audit_append, ts, actor, action, target, detail)

    async def audit_list(self, limit: int = 100, before_id: int | None = None) -> list[AuditRow]:
        return await self.run(self._store.audit_list, limit, before_id)
