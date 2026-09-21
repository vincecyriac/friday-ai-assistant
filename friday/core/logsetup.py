"""One stdout log handler for every node.

Under systemd, stdout is journald; it stamps every line itself, so the
timestamp is dropped when ``JOURNAL_STREAM`` is present. Calling this twice
replaces the previous handler rather than stacking a second one.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Mapping

PLAIN_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
JOURNAL_FORMAT = "%(levelname)-5s %(name)s: %(message)s"
_MARK = "_friday_handler"


def configure_logging(level: str = "INFO", *, env: Mapping[str, str] | None = None,
                      stream=None) -> None:
    env = os.environ if env is None else env
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(
        JOURNAL_FORMAT if "JOURNAL_STREAM" in env else PLAIN_FORMAT))
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
