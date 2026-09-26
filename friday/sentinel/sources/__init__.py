"""Least-privilege views onto the surfaces FRIDAY watches.

Each capability exposes only the verbs it is allowed to use. There is no
general-purpose request method on any of them, and nothing anywhere can send
mail: the IMAP account holds no SMTP credential (structural) and the Gmail
module contains no send path (enforced by an AST test).
"""

from friday.sentinel.sources.base import (AuthExpired, CalendarEvent, JiraIssue, MailAccount,
                                          MailMessage, PermissionDenied, SourceError, WatchItem,
                                          clean_text, decode_header_value, strip_html)
from friday.sentinel.sources.google import FRIDAY_STAMP, CalendarGuarded, GmailAccount
from friday.sentinel.sources.imap import ImapAccount
from friday.sentinel.sources.jira import DEFAULT_JQL, InvalidQuery, JiraReadOnly
from friday.sentinel.sources.oauth import SCOPES, GoogleOAuth

__all__ = ["DEFAULT_JQL", "FRIDAY_STAMP", "SCOPES", "AuthExpired", "CalendarEvent", "CalendarGuarded", "GmailAccount", "GoogleOAuth", "ImapAccount",
           "InvalidQuery", "JiraIssue", "JiraReadOnly", "MailAccount",
           "MailMessage", "PermissionDenied", "SourceError", "WatchItem", "clean_text",
           "decode_header_value", "strip_html"]
