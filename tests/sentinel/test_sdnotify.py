import os
import socket
import tempfile

import pytest

from friday.sentinel.sdnotify import sd_notify


def test_noop_without_notify_socket():
    assert sd_notify("READY=1", env={}) is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
def test_sends_datagram():
    with tempfile.TemporaryDirectory() as d:          # short path: AF_UNIX has a ~100-char limit
        path = os.path.join(d, "n.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(2.0)
        try:
            assert sd_notify("READY=1", env={"NOTIFY_SOCKET": path}) is True
            assert server.recv(64) == b"READY=1"
        finally:
            server.close()


def test_unreachable_socket_is_swallowed(tmp_path):
    assert sd_notify("READY=1", env={"NOTIFY_SOCKET": str(tmp_path / "missing.sock")}) is False
