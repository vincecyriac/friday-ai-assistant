"""A Gemini Live session that survives rotations, answers tool calls, and reports
everything as typed events.

Three behaviours here were expensive to learn and are the reason this lives in
one place:

* ``session.receive()`` yields ONE conversational turn and then ends. Without the
  outer loop the session is torn down after the first reply, and the resumed
  handle replays that turn — re-firing its tool calls on every rotation.
* A tool that raises must still produce a tool response. Skipping it leaves the
  turn open forever and the model re-issues the same calls after every resume.
* A resumption handle is dropped only when the handshake itself was refused
  while resuming. A live session that drops keeps its handle — that handle is
  exactly what resumes the conversation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from google.genai import types

from friday.core.live.events import (AudioOut, Connected, Disconnected, GoAway, Interrupted,
                                     LiveEvent, TextOut, ToolFinished, ToolStarted, Transcript,
                                     TurnComplete)
from friday.core.live.transport import AudioTransport

log = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bytes | None]]]


@dataclass(frozen=True)
class LiveConfig:
    model: str
    voice: str = "Aoede"
    system_instruction: str = ""
    tools: tuple[Mapping[str, Any], ...] = ()
    google_search: bool = False
    input_transcription: bool = False
    output_transcription: bool = False
    resume_handle: str | None = None
    max_backoff_s: float = 10.0


class LiveSession:
    def __init__(self, client: Any, config: LiveConfig, *, tool_handler: ToolHandler | None = None,
                 transport: AudioTransport | None = None,
                 on_handle: Callable[[str], None] | None = None,
                 logger: logging.Logger | None = None) -> None:
        self._client = client
        self._config = config
        self._tool_handler = tool_handler
        self._transport = transport
        self._on_handle = on_handle
        self._log = logger or log
        self._events: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._session: Any = None
        self._handle = config.resume_handle
        self._pending: set[asyncio.Task] = set()
        self.state = "idle"

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Connect, serve, rotate — until ``stop()``. A connect failure is an
        event and a retry, never an exception out of here."""
        failures = 0
        if self._transport is not None:
            await self._transport.start()
        try:
            while not self._stop.is_set():
                resuming = self._handle is not None
                self.state = "reconnecting" if resuming else "connecting"
                established = False
                try:
                    async with self._client.aio.live.connect(
                            model=self._config.model, config=self._connect_config()) as session:
                        established = True
                        self._session = session
                        self.state = "live"
                        failures = 0
                        self._emit(Connected(resumed=resuming))
                        await self._serve(session)
                    self._emit(Disconnected(reason="session ended", will_retry=not self._stop.is_set()))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failures += 1
                    retry = not self._stop.is_set()
                    self._emit(Disconnected(reason=str(e), will_retry=retry))
                    if resuming and not established:
                        # The handshake itself was refused: the handle is stale.
                        self._log.info("live: resume refused (%s); starting fresh", e)
                        self._handle = None
                        if retry:
                            await asyncio.sleep(0.5)
                        continue
                    self._log.warning("live: session error (%s)", e)
                    if retry:
                        await asyncio.sleep(self._backoff(failures))
                    continue
                finally:
                    self._session = None
                if not self._stop.is_set():
                    await asyncio.sleep(0.2)          # brief pause before rotating
        finally:
            self.state = "closed"
            self._session = None
            for task in list(self._pending):
                task.cancel()
            if self._pending:
                await asyncio.gather(*self._pending, return_exceptions=True)
            if self._transport is not None:
                try:
                    await self._transport.stop()
                except Exception:
                    pass
            self._events.put_nowait(None)

    def stop(self) -> None:
        self._stop.set()

    def _backoff(self, failures: int) -> float:
        return min(self._config.max_backoff_s, float(2 ** min(failures, 3)))

    async def _serve(self, session: Any) -> None:
        """Run the receive loop (and the transport pumps) until the session ends
        or ``stop()`` is called."""
        disconnected = asyncio.Event()
        tasks = [asyncio.create_task(self._receive_loop(session, disconnected))]
        tasks.extend(self._pump_tasks(disconnected))
        waiters = [asyncio.create_task(self._stop.wait()),
                   asyncio.create_task(disconnected.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            disconnected.set()
            for task in (*waiters, *tasks):
                task.cancel()
            await asyncio.gather(*waiters, *tasks, return_exceptions=True)

    def _pump_tasks(self, disconnected: asyncio.Event) -> list[asyncio.Task]:
        if self._transport is None:
            return []
        return [asyncio.create_task(self._mic_pump(disconnected))]

    async def _mic_pump(self, disconnected: asyncio.Event) -> None:
        """Transport → model. Ends on EOF or disconnect; never ends the session."""
        transport = self._transport
        assert transport is not None
        try:
            while not (self._stop.is_set() or disconnected.is_set()):
                chunk = await transport.read()
                if chunk is None:
                    return
                await self.send_audio(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.info("live: mic pump ended (%s)", e)

    # ---------------------------------------------------------------- config

    def _connect_config(self) -> types.LiveConnectConfig:
        c = self._config
        tools: list[types.Tool] = []
        if c.google_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        if c.tools:
            tools.append(types.Tool(function_declarations=[
                types.FunctionDeclaration(**dict(t)) for t in c.tools]))
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=c.voice))),
            system_instruction=types.Content(parts=[types.Part.from_text(text=c.system_instruction)]),
            tools=tools or None,
            input_audio_transcription=types.AudioTranscriptionConfig() if c.input_transcription else None,
            output_audio_transcription=types.AudioTranscriptionConfig() if c.output_transcription else None,
            # No "transparent": that flag is Vertex only, and the Developer API
            # refuses the whole connection when it is set. Sent on every connect
            # (handle=None simply starts a fresh resumable session) because this
            # config is what asks the server for SessionResumptionUpdates.
            session_resumption=types.SessionResumptionConfig(handle=self._handle),
        )

    # ---------------------------------------------------------------- events

    def _emit(self, event: LiveEvent) -> None:
        self._events.put_nowait(event)
        if self._transport is None:
            return
        if isinstance(event, AudioOut):
            self._spawn(self._transport.write(event.pcm))
        elif isinstance(event, Interrupted):
            self._spawn(self._transport.clear())

    def _spawn(self, coro) -> None:
        """Fire-and-forget on the running loop; a transport write must never
        block decoding or raise into it."""
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Every event until the session closes. One consumer is expected."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    # --------------------------------------------------------------- sending

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(lambda s: s.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")), "audio")

    async def send_video(self, jpeg: bytes) -> None:
        await self._send(lambda s: s.send_realtime_input(
            video=types.Blob(data=jpeg, mime_type="image/jpeg")), "video")

    async def send_text(self, text: str, *, turn_complete: bool = True) -> None:
        await self._send(lambda s: s.send_client_content(
            turns=[{"role": "user", "parts": [{"text": text}]}], turn_complete=turn_complete), "text")

    async def _send(self, call: Callable[[Any], Awaitable[None]], what: str) -> None:
        session = self._session
        if session is None:
            self._log.debug("live: dropped %s, no session", what)
            return
        try:
            await call(session)
        except Exception as e:
            self._log.debug("live: could not send %s: %s", what, e)

    # -------------------------------------------------------------- decoding

    async def _receive_loop(self, session: Any, disconnected: asyncio.Event) -> None:
        try:
            while not (self._stop.is_set() or disconnected.is_set()):
                async for message in session.receive():
                    if self._stop.is_set() or disconnected.is_set():
                        return
                    if await self._handle_message(session, message):
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.info("live: receive ended (%s)", e)
        finally:
            disconnected.set()

    async def _handle_message(self, session: Any, message: Any) -> bool:
        """Decode one server message. Returns True when the session must rotate."""
        if message.go_away is not None:
            self._emit(GoAway(time_left=getattr(message.go_away, "time_left", None)))
            return True

        content = message.server_content
        if content is not None:
            if content.interrupted:
                self._emit(Interrupted())
                return False
            for part in (content.model_turn.parts if content.model_turn else []) or []:
                if part.inline_data is not None and part.inline_data.data:
                    self._emit(AudioOut(pcm=part.inline_data.data))
                if part.text:
                    self._emit(TextOut(text=part.text))
            self._emit_transcript(content.input_transcription, "user")
            self._emit_transcript(content.output_transcription, "assistant")
            if content.turn_complete:
                self._emit(TurnComplete())

        update = message.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self._handle = update.new_handle
            if self._on_handle is not None:
                self._on_handle(update.new_handle)

        if message.tool_call is not None:
            await self._run_tools(session, message.tool_call.function_calls or [])
        return False

    def _emit_transcript(self, transcription: Any, role: str) -> None:
        if transcription is None or not transcription.text:
            return
        self._emit(Transcript(text=transcription.text, role=role,
                              final=bool(getattr(transcription, "finished", False))))

    async def _run_tools(self, session: Any, calls: list[Any]) -> None:
        responses = []
        for call in calls:
            name, args = call.name, dict(call.args or {})
            self._emit(ToolStarted(name=name, args=args))
            started = time.monotonic()
            image = None
            failed = False
            if self._tool_handler is None:
                output = f"[Tool error] {name}: no tool handler configured"
                failed = True
            else:
                try:
                    output, image = await self._tool_handler(name, args)
                except Exception as e:
                    output, failed = f"[Tool error] {name} failed: {e}", True
                    self._log.info("live: %s", output)
            output = str(output)
            if image:
                await self._send(lambda s, img=image: s.send_realtime_input(
                    video=types.Blob(data=img, mime_type="image/jpeg")), "tool image")
            self._emit(ToolFinished(name=name, output=output,
                                    ms=int((time.monotonic() - started) * 1000), failed=failed))
            responses.append(types.FunctionResponse(id=call.id, name=name,
                                                    response={"output": output}))
        if responses:
            # A tool that raised still answers: an unanswered turn never closes
            # and the model re-issues the call after every resume.
            try:
                await session.send_tool_response(function_responses=responses)
            except Exception as e:
                self._log.info("live: could not send tool response: %s", e)
