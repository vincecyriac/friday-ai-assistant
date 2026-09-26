import dataclasses

import pytest

from friday.sentinel.sources import (AuthExpired, CalendarEvent, JiraIssue, MailMessage,
                                     PermissionDenied, SourceError, WatchItem, clean_text,
                                     decode_header_value, strip_html)
from friday.sentinel.sources.base import SNIPPET_LIMIT


def _item(**kw):
    base = dict(source="email", external_id="gmail:abc", title="Subject", snippet="body",
                who="Ada <ada@example.com>", url="https://mail.example/1", ts=1.0, meta={})
    return WatchItem(**{**base, **kw})


def test_watch_item_id_is_source_qualified_and_serialises():
    item = _item()
    assert item.id == "email:gmail:abc"
    payload = item.to_dict()
    assert payload["source"] == "email" and payload["external_id"] == "gmail:abc"
    assert payload["meta"] == {} and payload["ts"] == 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.title = "no"


def test_two_accounts_with_the_same_raw_id_are_different_items():
    """A Gmail id and an IMAP UID can both be '12345'; they must not collide."""
    assert _item(external_id="gmail:12345").id != _item(external_id="imap:12345").id


def test_errors_form_the_expected_hierarchy():
    assert issubclass(AuthExpired, SourceError)
    assert not issubclass(PermissionDenied, SourceError)      # a refusal is not a failure


@pytest.mark.parametrize("raw,expected", [
    ("  hello   world \n", "hello world"),
    (b"bytes in", "bytes in"),
    (None, ""),
    ("", ""),
    ("tabs\tand\r\nnewlines", "tabs and newlines"),
    ("\x00control\x07chars", "controlchars"),
])
def test_clean_text_normalises(raw, expected):
    assert clean_text(raw) == expected


def test_clean_text_truncates_before_anything_downstream_sees_it():
    out = clean_text("x" * 10_000)
    assert len(out) == SNIPPET_LIMIT and out.endswith("…")
    assert clean_text("y" * 20, limit=10) == "yyyyyyyyy…"


def test_clean_text_survives_undecodable_bytes():
    assert clean_text(b"caf\xe9 noir") in ("caf noir", "café noir")


@pytest.mark.parametrize("raw,expected", [
    ("=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=", "Schöne Grüße"),
    ("=?utf-8?q?Re=3A_status?=", "Re: status"),
    ("plain subject", "plain subject"),
    (None, ""),
])
def test_decode_header_value(raw, expected):
    assert decode_header_value(raw) == expected


def test_an_undecodable_header_is_shown_as_it_arrived():
    assert "BOGUS" in decode_header_value("=?BOGUS?X?zz?=")


def test_strip_html_keeps_the_words_and_drops_the_markup():
    html = "<html><head><style>p{color:red}</style></head><body><p>Hello <b>you</b></p>" \
           "<script>evil()</script><br>Second line</body></html>"
    out = strip_html(html)
    assert "Hello you" in out and "Second line" in out
    assert "<" not in out and "evil()" not in out and "color:red" not in out
    assert strip_html("") == ""


def test_strip_html_unescapes_entities():
    assert strip_html("<p>A &amp; B &lt;tag&gt;&nbsp;end</p>") == "A & B <tag> end"


def test_source_dataclasses_are_frozen_value_objects():
    m = MailMessage(account="gmail", uid="1", subject="s", sender="a", snippet="x", ts=1.0,
                    url="", thread_id=None)
    c = CalendarEvent(id="e", summary="s", organiser="o", start=1.0, end=2.0, all_day=False,
                      created_by_friday=True, url="")
    j = JiraIssue(key="K-1", summary="s", who="w", priority="High", status="Blocked",
                  updated=1.0, url="", is_comment=False)
    assert (m.account, c.all_day, j.key) == ("gmail", False, "K-1")
    for obj in (m, c, j):
        with pytest.raises(dataclasses.FrozenInstanceError):
            obj.summary = "no"
