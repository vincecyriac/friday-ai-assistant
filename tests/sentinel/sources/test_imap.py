import email
import email.policy
from email.message import EmailMessage

import pytest

from friday.sentinel.sources import SourceError
from friday.sentinel.sources.imap import ImapAccount


def raw_message(subject="Status update", sender="Ada <ada@example.com>",
                plain="the plain body", html=None,
                date="Thu, 26 Sep 2026 09:30:00 +0200"):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = date
    if plain is not None:
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
    else:
        message.set_content(html or "", subtype="html")
    return message.as_bytes()


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: records commands, returns canned data."""

    seed = {}          # uids / messages / fail_on for the next instance
    last = None

    def __init__(self, host, port=993, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.commands: list[tuple] = []
        self.appended: list[tuple] = []
        self.logged_out = False
        self.uids = list(FakeIMAP.seed.get("uids", []))
        self.messages = dict(FakeIMAP.seed.get("messages", {}))
        self.fail_on = FakeIMAP.seed.get("fail_on")
        FakeIMAP.last = self

    def login(self, user, password):
        self.commands.append(("login", user))
        if self.fail_on == "login":
            raise OSError("auth failed")
        return "OK", [b"welcome"]

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, *[str(a) for a in args]))
        if self.fail_on == command.lower():
            raise OSError(f"{command} failed")
        if command.upper() == "SEARCH":
            return "OK", [b" ".join(self.uids)]
        if command.upper() == "FETCH":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            body = self.messages.get(uid)
            if body is None:
                return "NO", [None]
            return "OK", [(b"1 (UID " + uid.encode() + b" BODY[])", body), b")"]
        return "OK", [None]

    def append(self, mailbox, flags, date_time, message):
        self.commands.append(("append", mailbox, flags))
        self.appended.append((mailbox, flags, message))
        return "OK", [b"[APPENDUID 1 42] done"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture(autouse=True)
def reset_fake():
    FakeIMAP.seed, FakeIMAP.last = {}, None
    yield
    FakeIMAP.seed, FakeIMAP.last = {}, None


def account(**kw):
    return ImapAccount("imap.example.com", 993, "vince", "app-password",
                       connect=FakeIMAP, **kw)


async def test_list_unread_maps_messages_and_never_marks_them_read():
    FakeIMAP.seed = {"uids": [b"11", b"12"],
                     "messages": {"11": raw_message(), "12": raw_message(subject="Second")}}
    messages = await account().list_unread(limit=10)

    assert [m.subject for m in messages] == ["Second", "Status update"]     # newest first
    assert messages[0].account == "imap" and messages[0].uid == "12"
    assert messages[1].snippet == "the plain body"
    assert messages[0].ts > 0 and messages[0].url == ""
    imap = FakeIMAP.last
    fetches = [c for c in imap.commands if c[0] == "uid" and c[1].upper() == "FETCH"]
    assert fetches and all("BODY.PEEK[]" in str(c) for c in fetches)        # never sets \Seen
    assert ("select", "INBOX", True) in imap.commands                        # readonly
    assert imap.logged_out is True


async def test_html_only_mail_still_yields_a_snippet():
    FakeIMAP.seed = {"uids": [b"20"],
                     "messages": {"20": raw_message(plain=None, html="<p>Hello <b>there</b></p>")}}
    messages = await account().list_unread()
    assert messages[0].snippet == "Hello there"


async def test_encoded_headers_are_decoded():
    FakeIMAP.seed = {"uids": [b"21"],
                     "messages": {"21": raw_message(subject="=?UTF-8?B?U2Now7ZuZSBHcsO8w59l?=")}}
    messages = await account().list_unread()
    assert messages[0].subject == "Schöne Grüße"


async def test_a_huge_body_is_truncated():
    FakeIMAP.seed = {"uids": [b"22"], "messages": {"22": raw_message(plain="x" * 1_000_000)}}
    messages = await account().list_unread()
    assert len(messages[0].snippet) <= 500


async def test_since_uid_narrows_the_search():
    FakeIMAP.seed = {"uids": []}
    await account().list_unread(since_uid=100)
    search = [c for c in FakeIMAP.last.commands if c[1].upper() == "SEARCH"][0]
    assert "101:*" in " ".join(str(part) for part in search)


async def test_create_draft_appends_to_the_drafts_folder():
    uid = await account(drafts_folder="[Gmail]/Drafts").create_draft(
        "ada@example.com", "Re: status", "On it.")
    mailbox, flags, raw = FakeIMAP.last.appended[0]
    assert mailbox == "[Gmail]/Drafts" and "\\Draft" in flags
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed["To"] == "ada@example.com" and parsed["Subject"] == "Re: status"
    assert "On it." in parsed.get_content()
    assert uid == "42"


async def test_a_failure_is_a_source_error_without_the_password():
    FakeIMAP.seed = {"fail_on": "login"}
    with pytest.raises(SourceError) as excinfo:
        await account().list_unread()
    assert "app-password" not in str(excinfo.value)
