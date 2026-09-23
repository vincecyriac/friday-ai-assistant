"""Where a session's audio comes from and goes to.

A consumer that wants the session to pump audio for it passes a transport; one
that multiplexes its own audio (the desktop hub) passes none and calls
``send_audio`` itself.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioTransport(Protocol):
    async def start(self) -> None: ...
    async def read(self) -> bytes | None: ...      # 16 kHz mic PCM16; None ends the pump
    async def write(self, pcm: bytes) -> None: ...  # 24 kHz model PCM16
    async def clear(self) -> None: ...             # drop buffered playback (barge-in)
    async def stop(self) -> None: ...


class QueueTransport:
    """In-memory transport: tests and any caller happy to push and pull queues."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.written: list[bytes] = []
        self.clears = 0
        self.started = False
        self.stopped = False

    # ------------------------------------------------ producer side (caller)

    def feed(self, pcm: bytes) -> None:
        self._inbound.put_nowait(pcm)

    def feed_eof(self) -> None:
        self._inbound.put_nowait(None)

    # ---------------------------------------------- AudioTransport (session)

    async def start(self) -> None:
        self.started = True

    async def read(self) -> bytes | None:
        return await self._inbound.get()

    async def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    async def clear(self) -> None:
        self.clears += 1

    async def stop(self) -> None:
        self.stopped = True
        self._inbound.put_nowait(None)          # unblock a pending read
