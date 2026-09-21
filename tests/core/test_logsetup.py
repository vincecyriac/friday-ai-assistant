import logging

import pytest

from friday.core.logsetup import configure_logging


def _ours():
    return [h for h in logging.getLogger().handlers if getattr(h, "_friday_handler", False)]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for h in _ours():
        logging.getLogger().removeHandler(h)


def test_configure_is_idempotent():
    configure_logging("INFO", env={})
    configure_logging("DEBUG", env={})
    assert len(_ours()) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_plain_format_has_timestamp():
    configure_logging("INFO", env={})
    assert "asctime" in _ours()[0].formatter._fmt


def test_journal_format_has_no_timestamp():
    configure_logging("INFO", env={"JOURNAL_STREAM": "9:12345"})
    assert "asctime" not in _ours()[0].formatter._fmt


def test_level_is_case_insensitive():
    configure_logging("warning", env={})
    assert logging.getLogger().level == logging.WARNING
