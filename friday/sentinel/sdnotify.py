"""Minimal sd_notify(3): READY=1 / WATCHDOG=1 / STOPPING=1 over NOTIFY_SOCKET.

A no-op anywhere systemd did not start us (macOS, a foreground run), which
is why the daemon can call it unconditionally.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Mapping

log = logging.getLogger(__name__)
_warned = False


def sd_notify(state: str, *, env: Mapping[str, str] | None = None) -> bool:
    global _warned
    env = os.environ if env is None else env
    address = env.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):                 # Linux abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(address)
            sock.sendall(state.encode())
        return True
    except OSError as e:
        if not _warned:
            _warned = True
            log.warning("sd_notify to %s failed: %s", env.get("NOTIFY_SOCKET"), e)
        return False
