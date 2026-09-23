"""The sentinel's own voice: a Live session bridged to a dashboard WebSocket.

The browser owns the microphone and the speakers; this module owns the session,
the tools (the same seven the text assistant has) and the transcript that lands
in the conversation the user has open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from friday.core.config import ConfigError
from friday.core.live import (AudioOut, Connected, Disconnected, Interrupted, LiveConfig,
                              LiveSession, TextOut, ToolFinished, ToolStarted, Transcript,
                              TurnComplete)
from friday.sentinel.assistant import DEFAULT_TITLE, SYSTEM_PROMPT, ToolSet, title_from
from friday.sentinel.auth import User

log = logging.getLogger(__name__)


class WebSocketTransport:
    """AudioTransport over an aiohttp WebSocketResponse: binary frames both ways,
    one JSON frame to tell the browser to drop buffered playback."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.started = False

    def feed(self, pcm: bytes) -> None:
        self._inbound.put_nowait(pcm)

    def feed_eof(self) -> None:
        self._inbound.put_nowait(None)

    async def start(self) -> None:
        self.started = True

    async def read(self) -> bytes | None:
        return await self._inbound.get()

    async def write(self, pcm: bytes) -> None:
        try:
            await self._ws.send_bytes(pcm)
        except Exception as e:                      # the browser went away
            log.debug("voice: could not write audio: %s", e)

    async def clear(self) -> None:
        try:
            await self._ws.send_json({"type": "clear"})
        except Exception:
            pass

    async def stop(self) -> None:
        self.feed_eof()


class VoiceSession:
    """One browser voice session: Live engine + transport + transcript."""

    def __init__(self, services: Any, user: User, conversation_id: str, ws: Any):
        self._svc = services
        self._user = user
        self.conversation_id = conversation_id
        self._ws = ws
        self._transport = WebSocketTransport(ws)
        self._toolset = ToolSet(services, user)
        self._pending: dict[str, str] = {"user": "", "assistant": ""}
        self._live: LiveSession | None = None
        self._started_at = 0.0

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        try:
            model, client = self._svc.live_model(), self._svc.live_client()
        except ConfigError as e:
            await self._send({"type": "error",
                              "message": f"{e} — set the Gemini key under Settings → llm."})
            return
        config = LiveConfig(
            model=model,
            voice=self._svc.config.get("voice.name"),
            system_instruction=SYSTEM_PROMPT.format(node_id=self._svc.settings.node_id),
            tools=tuple(ToolSet.declarations),
            input_transcription=True,
            output_transcription=True,
        )
        self._live = LiveSession(client, config, tool_handler=self._call_tool,
                                 transport=self._transport, logger=log)
        self._started_at = time.monotonic()
        runner = asyncio.create_task(self._live.run())
        try:
            await self._consume(self._live)
        finally:
            self._live.stop()
            try:
                await asyncio.wait_for(runner, timeout=5)
            except (asyncio.TimeoutError, Exception):
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await self._svc.audit(f"user:{self._user.username}", "voice.session", None,
                                  {"conversation_id": self.conversation_id,
                                   "seconds": round(time.monotonic() - self._started_at, 1)})

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
        self._transport.feed_eof()

    # ------------------------------------------------------- browser → model

    def feed_audio(self, pcm: bytes) -> None:
        self._transport.feed(pcm)

    async def feed_text(self, text: str) -> None:
        if self._live is not None:
            await self._live.send_text(text)

    # ------------------------------------------------------- model → browser

    async def _consume(self, live: LiveSession) -> None:
        async for event in live.events():
            try:
                if isinstance(event, Transcript):
                    await self._on_transcript(event)
                elif isinstance(event, TurnComplete):
                    await self._flush("user")
                    await self._flush("assistant")
                    await self._send({"type": "turn_complete"})
                elif isinstance(event, TextOut):
                    self._pending["assistant"] += event.text
                elif isinstance(event, Interrupted):
                    await self._flush("assistant")
                elif isinstance(event, ToolStarted):
                    await self._send({"type": "tool", "phase": "start", "name": event.name,
                                      "args": event.args})
                elif isinstance(event, ToolFinished):
                    await self._send({"type": "tool", "phase": "done", "name": event.name,
                                      "output": event.output, "ms": event.ms})
                elif isinstance(event, Connected):
                    await self._send({"type": "state", "value": "live"})
                elif isinstance(event, Disconnected):
                    await self._send({"type": "state",
                                      "value": "reconnecting" if event.will_retry else "closed"})
                elif isinstance(event, AudioOut):
                    pass                            # the transport already wrote it
            except Exception as e:
                log.info("voice: event handling failed: %s", e)

    async def _on_transcript(self, event: Transcript) -> None:
        # Gemini streams a transcript as fragments; ``final`` marks the last one.
        # Accumulate, then flush — TurnComplete flushes too, in case the flag
        # never arrives.
        self._pending[event.role] = (self._pending.get(event.role) or "") + event.text
        await self._send({"type": "transcript", "role": event.role,
                          "text": self._pending[event.role], "final": event.final})
        if event.final:
            await self._flush(event.role)

    async def _flush(self, role: str) -> None:
        text = (self._pending.get(role) or "").strip()
        self._pending[role] = ""
        if not text:
            return
        conv = await self._svc.store.conversation_get(self.conversation_id)
        if conv is None:
            return
        await self._svc.store.message_append(self.conversation_id, role, text,
                                             via="voice", ts=time.time())
        if role == "user" and conv.title == DEFAULT_TITLE:
            await self._svc.store.conversation_touch(self.conversation_id, time.time(),
                                                     title=title_from(text))

    async def _send(self, obj: dict) -> None:
        try:
            await self._ws.send_json(obj)
        except Exception as e:
            log.debug("voice: could not send %s: %s", obj.get("type"), e)

    # ------------------------------------------------------------------ tool

    async def _call_tool(self, name: str, args: dict) -> tuple[str, bytes | None]:
        return await self._toolset.call(name, args), None
