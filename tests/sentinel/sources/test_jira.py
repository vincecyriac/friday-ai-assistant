import inspect

import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, SourceError
from friday.sentinel.sources.jira import DEFAULT_JQL, InvalidQuery, JiraReadOnly


def _issue(key="OPS-1", summary="Disk full", priority="Highest", status="Blocked",
           updated="2026-09-26T09:30:00.000+0000", reporter="Ada Lovelace"):
    return {"key": key, "fields": {"summary": summary,
                                   "priority": {"name": priority},
                                   "status": {"name": status},
                                   "updated": updated,
                                   "reporter": {"displayName": reporter}}}


async def _client(server):
    session = aiohttp.ClientSession()
    return session, JiraReadOnly(session, server.url, "vince@example.com", "token-1")


async def test_search_maps_issues_and_sends_basic_auth(jira_server):
    jira_server.issues = [_issue(), _issue(key="OPS-2", summary="Certificate expiring",
                                           priority="High", status="On Hold")]
    session, jira = await _client(jira_server)
    try:
        issues = await jira.search("assignee = currentUser()", limit=10)
    finally:
        await session.close()

    assert [i.key for i in issues] == ["OPS-1", "OPS-2"]
    first = issues[0]
    assert first.summary == "Disk full" and first.priority == "Highest" and first.status == "Blocked"
    assert first.who == "Ada Lovelace" and first.is_comment is False
    assert first.url == f"{jira_server.url}/browse/OPS-1"
    assert first.updated > 0
    call = jira_server.calls[0]
    assert call["method"] == "GET" and call["query"]["jql"] == "assignee = currentUser()"
    assert call["query"]["maxResults"] == "10"
    assert call["headers"]["Authorization"].startswith("Basic ")


async def test_the_class_exposes_no_write_verb():
    """Structural, not a promise: adding a write means adding a method."""
    source = inspect.getsource(JiraReadOnly)
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source, f"JiraReadOnly must not {verb}"
    assert not any(name.startswith(("_post", "_put", "_delete", "_patch")) for name in dir(JiraReadOnly))


async def test_only_get_ever_reaches_the_server(jira_server):
    jira_server.issues = [_issue()]
    session, jira = await _client(jira_server)
    try:
        await jira.search(DEFAULT_JQL)
    finally:
        await session.close()
    assert jira_server.methods == {"GET"}


async def test_a_bad_jql_is_invalid_query_not_a_retry(jira_server):
    jira_server.status = 400
    session, jira = await _client(jira_server)
    try:
        with pytest.raises(InvalidQuery) as excinfo:
            await jira.search("this is not jql")
    finally:
        await session.close()
    assert issubclass(InvalidQuery, SourceError)
    assert "unexpected token" in str(excinfo.value)          # Atlassian's own words, for the UI
    assert "token-1" not in str(excinfo.value)


@pytest.mark.parametrize("status,expected", [(401, AuthExpired), (403, AuthExpired),
                                             (500, SourceError), (502, SourceError)])
async def test_status_mapping(jira_server, status, expected):
    jira_server.status = status
    session, jira = await _client(jira_server)
    try:
        with pytest.raises(expected):
            await jira.search(DEFAULT_JQL)
    finally:
        await session.close()


async def test_a_malformed_issue_is_skipped_not_fatal(jira_server):
    jira_server.issues = [{"key": "OPS-9"}, _issue(key="OPS-10")]     # no fields at all
    session, jira = await _client(jira_server)
    try:
        issues = await jira.search(DEFAULT_JQL)
    finally:
        await session.close()
    assert [i.key for i in issues] == ["OPS-9", "OPS-10"]
    assert issues[0].summary == "" and issues[0].priority == "" and issues[0].updated == 0.0


async def test_default_jql_covers_assigned_blocked_flagged_and_mentions():
    for fragment in ("assignee = currentUser()", "statusCategory != Done", "Highest",
                     "Blocked", "On Hold", "flagged is not EMPTY", "updated >= -1d"):
        assert fragment in DEFAULT_JQL
