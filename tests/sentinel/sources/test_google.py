import base64
import datetime as dt
import time

import aiohttp
import pytest

from friday.sentinel.sources import AuthExpired, PermissionDenied, SourceError
from friday.sentinel.sources.google import FRIDAY_STAMP, CalendarGuarded, GmailAccount
from friday.sentinel.sources.oauth import GoogleOAuth


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def gmail_message(id="m1", subject="Status update", sender="Ada <ada@example.com>",
                  body_plain="the plain body", body_html=None, internal_ms=1_700_000_000_000,
                  thread="t1"):
    parts = []
    if body_plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(body_plain)}})
    if body_html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(body_html)}})
    return {"id": id, "threadId": thread, "internalDate": str(internal_ms),
            "snippet": "api snippet",
            "payload": {"headers": [{"name": "Subject", "value": subject},
                                    {"name": "From", "value": sender}],
                        "parts": parts}}


async def _gmail(server):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh", token_url=f"{server.url}/token")
    return session, GmailAccount(session, oauth, api_base=server.url)


async def _calendar(server):
    session = aiohttp.ClientSession()
    oauth = GoogleOAuth(session, "cid", "secret", "refresh", token_url=f"{server.url}/token")
    return session, CalendarGuarded(session, oauth, api_base=server.url)


# -------------------------------------------------------------------- gmail

async def test_list_unread_maps_messages(google_server):
    google_server.gmail["messages"] = [{"id": "m1"}]
    google_server.gmail["byid"] = {"m1": gmail_message()}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread(limit=5)
    finally:
        await session.close()

    assert len(messages) == 1
    m = messages[0]
    assert m.account == "gmail" and m.uid == "m1" and m.subject == "Status update"
    assert m.sender == "Ada <ada@example.com>" and m.snippet == "the plain body"
    assert m.thread_id == "t1" and m.ts == 1_700_000_000.0
    assert m.url == "https://mail.google.com/mail/u/0/#inbox/m1"
    listing = [c for c in google_server.calls if c["path"].endswith("/messages")][0]
    assert listing["query"]["q"] == "is:unread -in:chats" and listing["query"]["maxResults"] == "5"


async def test_html_only_mail_still_yields_a_snippet(google_server):
    """Most real mail has no text/plain part at all."""
    google_server.gmail["messages"] = [{"id": "m2"}]
    google_server.gmail["byid"] = {"m2": gmail_message(
        id="m2", body_plain=None, body_html="<p>Hello <b>there</b></p><script>x()</script>")}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].snippet == "Hello there"


async def test_a_message_with_no_body_falls_back_to_the_api_snippet(google_server):
    google_server.gmail["messages"] = [{"id": "m3"}]
    google_server.gmail["byid"] = {"m3": gmail_message(id="m3", body_plain=None)}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].snippet == "api snippet"


async def test_encoded_headers_are_decoded(google_server):
    google_server.gmail["messages"] = [{"id": "m4"}]
    google_server.gmail["byid"] = {"m4": gmail_message(
        id="m4", subject="=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=",
        sender="=?utf-8?q?Ada_Lovelace?= <ada@example.com>")}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert messages[0].subject == "Schöne Grüße"
    assert "Ada Lovelace" in messages[0].sender and "=?utf" not in messages[0].sender


async def test_a_huge_body_is_truncated_before_it_leaves_the_capability(google_server):
    google_server.gmail["messages"] = [{"id": "m5"}]
    google_server.gmail["byid"] = {"m5": gmail_message(id="m5", body_plain="x" * 2_000_000)}
    session, gmail = await _gmail(google_server)
    try:
        messages = await gmail.list_unread()
    finally:
        await session.close()
    assert len(messages[0].snippet) <= 500


async def test_create_draft_posts_a_draft_and_returns_its_id(google_server):
    session, gmail = await _gmail(google_server)
    try:
        draft_id = await gmail.create_draft("ada@example.com", "Re: status", "On it.", thread_id="t1")
    finally:
        await session.close()
    assert draft_id == "draft-1"
    post = [c for c in google_server.calls if c["method"] == "POST" and "drafts" in c["path"]][0]
    raw = post["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "===").decode()
    assert "To: ada@example.com" in decoded and "Subject: Re: status" in decoded
    assert "On it." in decoded and post["body"]["message"]["threadId"] == "t1"


@pytest.mark.parametrize("status,expected", [(401, AuthExpired), (403, AuthExpired),
                                             (500, SourceError)])
async def test_gmail_status_mapping(google_server, status, expected):
    google_server.status["gmail"] = status
    session, gmail = await _gmail(google_server)
    try:
        with pytest.raises(expected):
            await gmail.list_unread()
    finally:
        await session.close()


# ----------------------------------------------------------------- calendar

def gcal_event(id="e1", summary="Standup", start="2026-09-26T09:00:00+02:00",
               end="2026-09-26T09:15:00+02:00", all_day=False, stamped=False,
               organiser="Ada Lovelace"):
    when = ({"date": start[:10]} if all_day else {"dateTime": start})
    until = ({"date": end[:10]} if all_day else {"dateTime": end})
    event = {"id": id, "summary": summary, "start": when, "end": until,
             "organizer": {"displayName": organiser},
             "htmlLink": f"https://calendar.google.com/event?eid={id}"}
    if stamped:
        event["extendedProperties"] = {"private": {"created_by": FRIDAY_STAMP}}
    return event


async def test_list_maps_events_including_all_day(google_server):
    google_server.calendar["events"] = [gcal_event(),
                                        gcal_event(id="e2", summary="Conference", all_day=True)]
    session, cal = await _calendar(google_server)
    try:
        events = await cal.list(0.0, 1e10)
    finally:
        await session.close()
    assert [e.summary for e in events] == ["Standup", "Conference"]
    assert events[0].all_day is False and events[0].start > 0 and events[0].end > events[0].start
    assert events[1].all_day is True and events[1].start > 0      # 'date', not 'dateTime'
    assert events[0].created_by_friday is False


async def test_create_stamps_the_event_as_friday_made(google_server):
    session, cal = await _calendar(google_server)
    try:
        await cal.create("Reminder", 1_700_000_000.0, 1_700_003_600.0, description="from FRIDAY")
    finally:
        await session.close()
    body = google_server.calendar["created"][0]
    assert body["extendedProperties"]["private"]["created_by"] == FRIDAY_STAMP
    assert body["summary"] == "Reminder" and "dateTime" in body["start"]


async def test_update_and_delete_refuse_an_event_friday_did_not_create(google_server):
    google_server.calendar["byid"] = {"human": gcal_event(id="human", stamped=False)}
    session, cal = await _calendar(google_server)
    try:
        with pytest.raises(PermissionDenied):
            await cal.update("human", summary="hijacked")
        with pytest.raises(PermissionDenied):
            await cal.delete("human")
    finally:
        await session.close()
    # The refusal happens before any write is attempted.
    assert not any(c["method"] in ("PATCH", "DELETE") for c in google_server.calls)


async def test_update_and_delete_allow_a_stamped_event(google_server):
    google_server.calendar["byid"] = {"mine": gcal_event(id="mine", stamped=True)}
    session, cal = await _calendar(google_server)
    try:
        await cal.update("mine", summary="moved")
        await cal.delete("mine")
    finally:
        await session.close()
    assert {c["method"] for c in google_server.calls} >= {"PATCH", "DELETE"}


async def test_in_meeting_is_true_only_inside_a_timed_event(google_server):
    now = time.time()
    google_server.calendar["events"] = [
        {"id": "now", "summary": "Live", "start": {"dateTime": _iso(now - 300)},
         "end": {"dateTime": _iso(now + 300)}, "organizer": {}, "htmlLink": ""}]
    session, cal = await _calendar(google_server)
    try:
        assert await cal.in_meeting(now) is True
        assert await cal.in_meeting(now + 3600) is False
    finally:
        await session.close()


async def test_an_all_day_event_is_not_a_meeting(google_server):
    """A week-long 'Conference' entry must not silence the phone for a week."""
    now = time.time()
    google_server.calendar["events"] = [gcal_event(id="ooo", summary="Conference", all_day=True,
                                                   start=_iso(now - 86400), end=_iso(now + 86400))]
    session, cal = await _calendar(google_server)
    try:
        assert await cal.in_meeting(now) is False
    finally:
        await session.close()
