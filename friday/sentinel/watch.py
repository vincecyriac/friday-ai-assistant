"""What FRIDAY watches, and the loop that watches it.

A monitor polls one surface and returns normalised items; the runner decides
whether to poll at all (the dashboard switch), stores what is new (the insert is
the dedupe) and publishes it. A source that fails parks itself — degraded for
something worth retrying, needs_reauth for something only a human can fix — and
never takes the daemon or the other sources with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import aiohttp

from friday.core.events import Event
from friday.sentinel.sources import (AuthExpired, CalendarEvent, JiraIssue, MailMessage,
                                     SourceError, WatchItem, clean_text)
from friday.sentinel.sources.google import CalendarGuarded, GmailAccount
from friday.sentinel.sources.imap import ImapAccount
from friday.sentinel.sources.jira import InvalidQuery, JiraReadOnly
from friday.sentinel.sources.oauth import GoogleOAuth

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 60.0
BOOKKEEPING_RETRY_S = 60.0        # after a failure outside the poll itself


@dataclass
class WatchContext:
    services: Any
    store: Any
    bus: Any
    logger: logging.Logger


class Monitor(Protocol):
    name: str
    interval_key: str
    configured: bool

    async def poll(self, ctx: WatchContext) -> list[WatchItem]: ...


# ----------------------------------------------------------------- sources

class Sources:
    """Builds capabilities from the current vault values, sharing one HTTP
    session and one OAuth token cache. Rebuilt cheaply per poll so a credential
    edited in the dashboard takes effect on the next tick."""

    def __init__(self, services: Any) -> None:
        self._svc = services
        self._session: aiohttp.ClientSession | None = None
        self._oauth: GoogleOAuth | None = None
        self._oauth_key = ""

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _get(self, key: str) -> str:
        value = self._svc.config.get(key)
        return "" if value is None else str(value).strip()

    def _google_oauth(self) -> GoogleOAuth | None:
        client_id = self._get("sources.google.client_id")
        secret = self._get("sources.google.client_secret")
        refresh = self._get("sources.google.refresh_token")
        if not (client_id and secret and refresh):
            return None
        oauth = GoogleOAuth(self._http(), client_id, secret, refresh)
        if self._oauth is None or oauth.credentials_key != self._oauth_key:
            self._oauth, self._oauth_key = oauth, oauth.credentials_key
        return self._oauth                       # keep the cached access token across polls

    def gmail(self) -> GmailAccount | None:
        oauth = self._google_oauth()
        return None if oauth is None else GmailAccount(self._http(), oauth)

    def calendar(self) -> CalendarGuarded | None:
        oauth = self._google_oauth()
        return None if oauth is None else CalendarGuarded(self._http(), oauth)

    def imap(self) -> ImapAccount | None:
        host, user = self._get("sources.imap.host"), self._get("sources.imap.user")
        password = self._get("sources.imap.password")
        if not (host and user and password):
            return None
        return ImapAccount(host, int(self._svc.config.get("sources.imap.port") or 993),
                           user, password,
                           drafts_folder=self._get("sources.imap.drafts_folder") or "Drafts")

    def jira(self) -> JiraReadOnly | None:
        base = self._get("sources.jira.base_url")
        email_address = self._get("sources.jira.email")
        token = self._get("sources.jira.api_token")
        if not (base and email_address and token):
            return None
        return JiraReadOnly(self._http(), base, email_address, token)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------- monitors

class EmailMonitor:
    name = "email"
    interval_key = "sources.email_interval_s"

    def __init__(self, gmail: Callable[[], Any], imap: Callable[[], Any]) -> None:
        self._gmail, self._imap = gmail, imap
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        gmail, imap = self._gmail(), self._imap()
        self.configured = bool(gmail or imap)
        items: list[WatchItem] = []
        if gmail is not None:
            items += [self._to_item(m) for m in await gmail.list_unread(limit=25)]
        if imap is not None:
            cursor = int(await ctx.store.kv_get("watch.cursor.imap") or 0)
            messages = await imap.list_unread(limit=25, since_uid=cursor)
            items += [self._to_item(m) for m in messages]
            highest = max((int(m.uid) for m in messages if m.uid.isdigit()), default=cursor)
            if highest > cursor:
                await ctx.store.kv_set("watch.cursor.imap", highest)
        return items

    @staticmethod
    def _to_item(message: MailMessage) -> WatchItem:
        # The account qualifies the id: a Gmail id and an IMAP UID can both be
        # "12345", and one real message must never suppress the other.
        return WatchItem(source="email", external_id=f"{message.account}:{message.uid}",
                         title=message.subject or "(no subject)", snippet=message.snippet,
                         who=message.sender, url=message.url, ts=message.ts,
                         meta={"account": message.account, "thread_id": message.thread_id})


class CalendarMonitor:
    name = "calendar"
    interval_key = "sources.calendar_interval_s"

    def __init__(self, calendar: Callable[[], Any]) -> None:
        self._calendar = calendar
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        calendar = self._calendar()
        self.configured = calendar is not None
        if calendar is None:
            return []
        horizon = float(ctx.services.config.get("sources.calendar_horizon_min") or 120) * 60
        now = time.time()
        events = await calendar.list(now, now + horizon)
        return [self._to_item(e) for e in events]

    @staticmethod
    def _to_item(event: CalendarEvent) -> WatchItem:
        return WatchItem(source="calendar", external_id=event.id, title=event.summary,
                         snippet=clean_text(f"{event.organiser} — starts "
                                            f"{time.strftime('%H:%M', time.localtime(event.start))}"),
                         who=event.organiser, url=event.url, ts=event.start,
                         meta={"start": event.start, "end": event.end, "all_day": event.all_day,
                               "created_by_friday": event.created_by_friday})


class JiraMonitor:
    name = "jira"
    interval_key = "sources.jira_interval_s"

    def __init__(self, jira: Callable[[], Any]) -> None:
        self._jira = jira
        self.configured = False

    async def poll(self, ctx: WatchContext) -> list[WatchItem]:
        jira = self._jira()
        self.configured = jira is not None
        if jira is None:
            return []
        jql = str(ctx.services.config.get("sources.jira.jql") or "")
        issues = await jira.search(jql, limit=50)
        cursor = float(await ctx.store.kv_get("watch.cursor.jira") or 0.0)
        newest = max((i.updated for i in issues), default=cursor)
        if newest > cursor:
            await ctx.store.kv_set("watch.cursor.jira", newest)
        return [self._to_item(i) for i in issues]

    @staticmethod
    def _to_item(issue: JiraIssue) -> WatchItem:
        return WatchItem(source="jira", external_id=issue.key,
                         title=f"{issue.key}: {issue.summary}",
                         snippet=clean_text(f"{issue.status} · {issue.priority} · {issue.who}"),
                         who=issue.who, url=issue.url, ts=issue.updated,
                         meta={"key": issue.key, "priority": issue.priority,
                               "status": issue.status, "is_comment": issue.is_comment})


# ------------------------------------------------------------------ runner

@dataclass
class _State:
    state: str = "watching"
    last_poll: float = 0.0
    last_error: str = ""
    failures: int = 0


class WatchRunner:
    """One supervised task that polls every monitor on its own schedule."""

    name = "watch"

    def __init__(self, services: Any, monitors: list[Monitor] | None = None, *,
                 interval_override_s: float | None = None) -> None:
        self._svc = services
        self._sources = Sources(services)
        # Registry intervals have a 30 s floor, which no test can wait for. The
        # override exists so tests can drive the loop; production passes None.
        self._override = interval_override_s
        self._monitors = monitors if monitors is not None else [
            EmailMonitor(self._sources.gmail, self._sources.imap),
            CalendarMonitor(self._sources.calendar),
            JiraMonitor(self._sources.jira),
        ]
        self._states: dict[str, _State] = {m.name: _State() for m in self._monitors}
        self.polls_completed = 0

    def status(self) -> dict[str, dict]:
        midnight = time.time() - (time.time() % 86400)
        counts = getattr(self, "_counts", {})
        return {m.name: {"state": self._states[m.name].state,
                         "enabled": bool(self._svc.config.get(f"controls.monitors.{m.name}")),
                         "configured": bool(getattr(m, "configured", False)),
                         "last_poll": self._states[m.name].last_poll,
                         "last_error": self._states[m.name].last_error,
                         "items_today": counts.get(m.name, 0)}
                for m in self._monitors}

    async def run(self, ctx: Any) -> None:
        watch_ctx = WatchContext(self._svc, self._svc.store, self._svc.bus, ctx.logger)
        try:
            # A task group, not gather: if one loop does escape, its siblings are
            # cancelled instead of polling on against the session we close below
            # while the supervisor starts a second full set of them.
            async with asyncio.TaskGroup() as group:
                for monitor in self._monitors:
                    group.create_task(self._loop(monitor, watch_ctx), name=f"watch:{monitor.name}")
        finally:
            await self._sources.close()

    async def _loop(self, monitor: Monitor, ctx: WatchContext) -> None:
        state = self._states[monitor.name]
        while True:
            try:
                if not bool(self._svc.config.get(f"controls.monitors.{monitor.name}")):
                    state.state = "disabled"
                else:
                    await self._tick(monitor, ctx, state)
                delay = self._override or self._interval(monitor, state)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Everything around the poll — the switch, the counts, the bus, the
                # interval — runs outside _tick's guard. A loop that exits here is a
                # source that has silently stopped watching while the card still
                # says "watching", so log it and take the next tick.
                ctx.logger.exception("watch: %s failed outside the poll", monitor.name)
                delay = self._override or BOOKKEEPING_RETRY_S
            await asyncio.sleep(delay)

    def _interval(self, monitor: Monitor, state: _State) -> float:
        base = float(self._svc.config.get(monitor.interval_key) or 300.0)
        if state.failures == 0:
            return base
        cap = float(self._svc.config.get("sources.max_backoff_s") or 900.0)
        if state.state == "needs_reauth":
            return cap                       # only a human can fix this; stop hammering
        return min(cap, base * (2 ** min(state.failures, 5)))

    async def _tick(self, monitor: Monitor, ctx: WatchContext, state: _State) -> None:
        previous = state.state
        try:
            async with asyncio.timeout(POLL_TIMEOUT_S):
                items = await monitor.poll(ctx)
        except asyncio.CancelledError:
            raise
        except AuthExpired as e:
            # The dashboard shows last_error verbatim, so it carries the remedy —
            # unless the source already named it, which Google's does.
            reason = str(e)
            if "google-auth" not in reason:
                reason = f"{reason} — run friday-sentinel google-auth"
            await self._fail(monitor, state, previous, "needs_reauth", "auth", reason)
            return
        except InvalidQuery as e:
            # A query the user typed. Retrying it changes nothing; showing it does.
            await self._fail(monitor, state, previous, "degraded", "query", str(e))
            return
        except (SourceError, asyncio.TimeoutError) as e:
            message = str(e) or f"{monitor.name} poll timed out"
            await self._fail(monitor, state, previous, "degraded", "transient", message)
            return
        except Exception as e:
            await self._fail(monitor, state, previous, "degraded", "transient",
                             f"{type(e).__name__}: {e}")
            return

        state.state = "watching" if getattr(monitor, "configured", True) else "unconfigured"
        state.last_poll, state.last_error, state.failures = time.time(), "", 0
        self.polls_completed += 1
        await self._absorb(monitor, ctx, items)

    async def _absorb(self, monitor: Monitor, ctx: WatchContext, items: list[Any]) -> None:
        now = time.time()
        for item in items:
            if not isinstance(item, WatchItem):
                ctx.logger.info("watch: %s returned a non-item, skipping", monitor.name)
                continue
            try:
                if await ctx.store.watch_item_add(item, now):
                    await ctx.bus.publish(Event(type="monitor.item",
                                                source=self._svc.settings.node_id,
                                                payload=item.to_dict()))
            except Exception as e:
                ctx.logger.warning("watch: could not record %s: %s", item.id, e)
        self._counts = await ctx.store.watch_counts(now - (now % 86400))

    async def _fail(self, monitor: Monitor, state: _State, previous: str,
                    new_state: str, kind: str, message: str) -> None:
        state.state, state.last_error = new_state, message
        state.failures += 1
        state.last_poll = time.time()
        self.polls_completed += 1
        if previous == new_state:
            return                          # one event per transition, not per tick
        await self._svc.bus.publish(Event(type="monitor.error",
                                          source=self._svc.settings.node_id,
                                          payload={"source": monitor.name, "kind": kind,
                                                   "message": message}))
