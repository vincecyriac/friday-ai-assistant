import asyncio
import logging
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from friday.sentinel.handlers import HandlerContext
from friday.sentinel.sources import AuthExpired, SourceError, WatchItem
from friday.sentinel.sources.jira import InvalidQuery
from friday.sentinel.watch import CalendarMonitor, EmailMonitor, JiraMonitor, WatchContext, WatchRunner


def item(source="email", external_id="gmail:1", ts=100.0, **kw):
    base = dict(title="Subject", snippet="body", who="Ada", url="", meta={})
    return WatchItem(source=source, external_id=external_id, ts=ts, **{**base, **kw})


class FakeMonitor:
    """A Monitor whose behaviour each test scripts."""

    def __init__(self, name="email", results=None):
        self.name = name
        self.interval_key = "sources.email_interval_s"
        self.results = list(results or [])
        self.polls = 0

    async def poll(self, ctx):
        self.polls += 1
        if not self.results:
            return []
        step = self.results.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
async def rig(services):
    ctx = HandlerContext(settings=services.settings, store=services.store, bus=services.bus,
                         logger=logging.getLogger("test.watch"))
    # Poll fast and switch everything on.
    await services.config.set_many({"sources.email_interval_s": 30, "controls.monitors.email": True,
                                    "controls.monitors.calendar": True,
                                    "controls.monitors.jira": True}, actor="test")
    return SimpleNamespace(services=services, ctx=ctx)


async def _run_once(runner, ctx, *, ticks=1, timeout=3.0):
    """Let the runner complete `ticks` polls, then stop it."""
    task = asyncio.create_task(runner.run(ctx))
    deadline = time.monotonic() + timeout
    try:
        while runner.polls_completed < ticks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ------------------------------------------------------------------ runner

async def test_new_items_are_stored_once_and_published_once(rig):
    monitor = FakeMonitor(results=[[item(), item(external_id="gmail:2")], [item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=2)

    rows = await rig.services.store.watch_items_list()
    assert {r.id for r in rows} == {"email:gmail:1", "email:gmail:2"}
    events = await rig.services.store.list_events(type="monitor.item", limit=50)
    assert len(events) == 2                       # the repeat on tick two published nothing
    payload = events[0].event.payload
    assert payload["source"] == "email" and payload["title"] == "Subject"


async def test_two_accounts_with_the_same_raw_id_both_land(rig):
    monitor = FakeMonitor(results=[[item(external_id="gmail:12345"),
                                    item(external_id="imap:12345")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    assert len(await rig.services.store.watch_items_list()) == 2
    assert len(await rig.services.store.list_events(type="monitor.item", limit=50)) == 2


async def test_a_disabled_monitor_is_not_polled(rig):
    await rig.services.config.set_many({"controls.monitors.email": False}, actor="test")
    monitor = FakeMonitor(results=[[item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    task = asyncio.create_task(runner.run(rig.ctx))
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert monitor.polls == 0
    assert runner.status()["email"]["state"] == "disabled"


async def test_the_switch_takes_effect_without_a_restart(rig):
    await rig.services.config.set_many({"controls.monitors.email": False}, actor="test")
    monitor = FakeMonitor(results=[[item()], [item(external_id="gmail:2")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    task = asyncio.create_task(runner.run(rig.ctx))
    try:
        await asyncio.sleep(0.08)
        assert monitor.polls == 0
        await rig.services.config.set_many({"controls.monitors.email": True}, actor="test")
        deadline = time.monotonic() + 3
        while monitor.polls == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert monitor.polls >= 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_transient_failure_degrades_only_that_source(rig):
    bad = FakeMonitor(name="jira", results=[SourceError("Atlassian said 502"), [item(source="jira",
                                                                                    external_id="OPS-1")]])
    bad.interval_key = "sources.jira_interval_s"
    good = FakeMonitor(results=[[item()], [item(external_id="gmail:2")]])
    runner = WatchRunner(rig.services, monitors=[bad, good], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=3)

    assert runner.status()["email"]["state"] == "watching"
    errors = await rig.services.store.list_events(type="monitor.error", limit=10)
    assert errors and errors[0].event.payload["source"] == "jira"
    assert errors[0].event.payload["kind"] == "transient"
    assert {r.source for r in await rig.services.store.watch_items_list()} >= {"email"}


async def test_auth_failure_parks_the_source_and_says_what_to_do(rig):
    # A revoked token fails every poll, not just the first: the fake must too, or the
    # next tick "recovers" the source out of the state under test.
    monitor = FakeMonitor(results=[AuthExpired("google refused the refresh token")] * 20)
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    state = runner.status()["email"]
    assert state["state"] == "needs_reauth" and "google-auth" in state["last_error"]
    assert state["last_error"].count("google-auth") == 1      # not stuttered onto Google's own text
    errors = await rig.services.store.list_events(type="monitor.error", limit=10)
    assert errors[0].event.payload["kind"] == "auth"


async def test_a_bad_jql_parks_like_an_auth_failure(rig):
    monitor = FakeMonitor(name="jira", results=[InvalidQuery("invalid JQL: unexpected token")] * 20)
    monitor.interval_key = "sources.jira_interval_s"
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    state = runner.status()["jira"]
    assert state["state"] == "degraded" and "invalid JQL" in state["last_error"]


async def test_one_error_event_per_transition_not_per_tick(rig):
    monitor = FakeMonitor(results=[SourceError("down")] * 4)
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=3)
    errors = await rig.services.store.list_events(type="monitor.error", limit=20)
    assert len(errors) == 1                       # an hour of downtime is one event, not sixty


async def test_recovery_clears_the_state(rig):
    monitor = FakeMonitor(results=[SourceError("down"), [item()]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx, ticks=2)
    assert runner.status()["email"]["state"] == "watching"
    assert runner.status()["email"]["last_error"] == ""


async def test_a_malformed_item_is_skipped_not_fatal(rig):
    monitor = FakeMonitor(results=[[item(), "not an item", item(external_id="gmail:3")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    assert {r.id for r in await rig.services.store.watch_items_list()} == {"email:gmail:1", "email:gmail:3"}


async def test_status_reports_every_monitor_for_the_dashboard(rig):
    runner = WatchRunner(rig.services, monitors=[FakeMonitor(), FakeMonitor(name="jira")],
                         interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    status = runner.status()
    assert set(status) == {"email", "jira"}
    assert set(status["email"]) == {"state", "enabled", "configured", "last_poll", "last_error",
                                    "items_today"}


# --------------------------------------------------------------- monitors

class FakeGmail:
    name = "gmail"

    def __init__(self, messages):
        self._messages = messages

    async def list_unread(self, limit=25):
        return list(self._messages)


class FakeImap(FakeGmail):
    name = "imap"

    async def list_unread(self, limit=25, since_uid=0):
        self.since_uid = since_uid
        return [m for m in self._messages if int(m.uid) > since_uid]


async def test_email_monitor_qualifies_ids_by_account(rig):
    from friday.sentinel.sources import MailMessage

    gmail = FakeGmail([MailMessage("gmail", "12345", "Work", "boss@x", "hi", 10.0, "u", "t")])
    imap = FakeImap([MailMessage("imap", "12345", "Personal", "mum@x", "hello", 20.0, "", None)])
    monitor = EmailMonitor(lambda: gmail, lambda: imap)
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    assert {i.external_id for i in items} == {"gmail:12345", "imap:12345"}
    assert {i.id for i in items} == {"email:gmail:12345", "email:imap:12345"}
    assert all(i.source == "email" for i in items)
    assert {i.meta["account"] for i in items} == {"gmail", "imap"}


async def test_email_monitor_advances_the_imap_cursor(rig):
    from friday.sentinel.sources import MailMessage

    imap = FakeImap([MailMessage("imap", "7", "a", "x", "s", 1.0, "", None),
                     MailMessage("imap", "9", "b", "x", "s", 2.0, "", None)])
    monitor = EmailMonitor(lambda: None, lambda: imap)
    ctx = WatchContext(rig.services, rig.services.store, rig.services.bus, logging.getLogger("t"))
    await monitor.poll(ctx)
    assert await rig.services.store.kv_get("watch.cursor.imap") == 9
    await monitor.poll(ctx)
    assert imap.since_uid == 9                    # the next search starts after the last UID


async def test_calendar_monitor_uses_the_configured_horizon(rig):
    from friday.sentinel.sources import CalendarEvent

    captured = {}

    class FakeCalendar:
        async def list(self, time_min, time_max):
            captured["window"] = (time_min, time_max)
            return [CalendarEvent("e1", "Standup", "Ada", time.time() + 600, time.time() + 1200,
                                  False, False, "https://cal/e1")]

    await rig.services.config.set_many({"sources.calendar_horizon_min": 30}, actor="test")
    monitor = CalendarMonitor(lambda: FakeCalendar())
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    span = captured["window"][1] - captured["window"][0]
    assert 1750 <= span <= 1850                   # ~30 minutes ahead
    assert items[0].source == "calendar" and items[0].title == "Standup"
    assert items[0].meta["all_day"] is False


async def test_jira_monitor_runs_the_configured_jql_and_advances_its_cursor(rig):
    from friday.sentinel.sources import JiraIssue

    seen = {}

    class FakeJira:
        async def search(self, jql, limit=50):
            seen["jql"] = jql
            return [JiraIssue("OPS-1", "Disk full", "Ada", "Highest", "Blocked", 500.0,
                              "https://j/OPS-1", False)]

    await rig.services.config.set_many({"sources.jira.jql": "assignee = currentUser()"}, actor="test")
    monitor = JiraMonitor(lambda: FakeJira())
    ctx = WatchContext(rig.services, rig.services.store, rig.services.bus, logging.getLogger("t"))
    items = await monitor.poll(ctx)
    assert seen["jql"] == "assignee = currentUser()"
    assert items[0].source == "jira" and items[0].external_id == "OPS-1"
    assert items[0].title == "OPS-1: Disk full" and items[0].meta["priority"] == "Highest"
    assert await rig.services.store.kv_get("watch.cursor.jira") == 500.0


async def test_an_unconfigured_source_yields_nothing_and_is_marked(rig):
    monitor = EmailMonitor(lambda: None, lambda: None)
    items = await monitor.poll(WatchContext(rig.services, rig.services.store, rig.services.bus,
                                            logging.getLogger("t")))
    assert items == []
    assert monitor.configured is False


# ------------------------------------------------------- the loop must not die

async def test_a_store_hiccup_does_not_kill_the_loop(rig, monkeypatch):
    """Bookkeeping after a good poll (counts, the bus) runs outside the poll's own
    guard. A loop that exits there is a source that has silently stopped watching
    while the Watching card still reads 'watching'."""
    import sqlite3

    calls = {"n": 0}
    real = rig.services.store.watch_counts

    async def flaky(since_ts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(since_ts)

    monkeypatch.setattr(rig.services.store, "watch_counts", flaky)
    monitor = FakeMonitor(results=[[item()], [item(external_id="gmail:2")],
                                   [item(external_id="gmail:3")]])
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    task = asyncio.create_task(runner.run(rig.ctx))
    try:
        deadline = time.monotonic() + 3
        while monitor.polls < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert monitor.polls >= 3 and not task.done()
        assert {r.id for r in await rig.services.store.watch_items_list()} >= {"email:gmail:2"}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_dying_loop_does_not_leave_its_siblings_polling(rig, monkeypatch):
    """If a loop ever does exit, run() must take the others with it. An orphan keeps
    polling against the session run() just closed, while the daemon's supervisor
    starts a whole second set of loops."""
    good = FakeMonitor(name="jira")
    good.interval_key = "sources.jira_interval_s"
    bad = FakeMonitor()
    runner = WatchRunner(rig.services, monitors=[bad, good], interval_override_s=0.01)
    real_loop = runner._loop

    async def loop(monitor, ctx):
        if monitor is bad:
            await asyncio.sleep(0.05)
            raise RuntimeError("this loop died")
        await real_loop(monitor, ctx)

    monkeypatch.setattr(runner, "_loop", loop)
    task = asyncio.create_task(runner.run(rig.ctx))
    with pytest.raises((RuntimeError, BaseExceptionGroup)):
        await asyncio.wait_for(task, 3)
    settled = good.polls
    await asyncio.sleep(0.1)
    assert good.polls == settled


async def test_a_source_that_already_names_the_remedy_is_not_repeated(rig):
    """GmailAccount's own AuthExpired says 'run friday-sentinel google-auth'. The card
    must not read '… google-auth — run friday-sentinel google-auth'."""
    monitor = FakeMonitor(results=[AuthExpired(
        "google refused the access token; run 'friday-sentinel google-auth'")] * 20)
    runner = WatchRunner(rig.services, monitors=[monitor], interval_override_s=0.01)
    await _run_once(runner, rig.ctx)
    assert runner.status()["email"]["last_error"].count("google-auth") == 1
