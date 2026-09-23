"""A scripted stand-in for google-genai's live session.

The real ``session.receive()`` yields ONE conversational turn and then ends;
the session object stays usable and the caller calls receive() again. The fake
models exactly that: each scripted "turn" is a list of messages, and once the
script is exhausted receive() blocks until the connection is closed.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from google.genai import types


# ------------------------------------------------------------ message builders

def _content(**kw):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


def audio_msg(pcm: bytes):
    return _content(model_turn=types.Content(role="model", parts=[
        types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))]))


def text_msg(text: str):
    return _content(model_turn=types.Content(role="model", parts=[types.Part(text=text)]))


def interrupt_msg():
    return _content(interrupted=True)


def turn_complete_msg():
    return _content(turn_complete=True)


def transcript_msg(*, inp=None, out=None, final=True):
    kw = {}
    if inp is not None:
        kw["input_transcription"] = types.Transcription(text=inp, finished=final)
    if out is not None:
        kw["output_transcription"] = types.Transcription(text=out, finished=final)
    return _content(**kw)


def tool_msg(name: str, args: dict, id: str = "c1"):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=id, name=name, args=args)]))


def tools_msg(*calls):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=f"c{i}", name=n, args=a)
                        for i, (n, a) in enumerate(calls)]))


def handle_msg(handle: str, resumable: bool = True):
    return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(
        new_handle=handle, resumable=resumable))


def goaway_msg(time_left: str | None = "5s"):
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=time_left))


# ------------------------------------------------------------------- the fake

class FakeSession:
    def __init__(self, client):
        self._client = client
        self._closed = asyncio.Event()

    async def receive(self):
        if self._client.turns:
            for message in self._client.turns.pop(0):
                if isinstance(message, Exception):
                    raise message
                yield message
            return                                   # one turn, then the caller loops
        await self._closed.wait()                    # idle session: stay open

    def close_now(self):
        self._closed.set()

    async def send_realtime_input(self, *, audio=None, video=None, text=None, **kw):
        if audio is not None:
            self._client.sent_audio.append(bytes(audio.data))
        if video is not None:
            self._client.sent_video.append(bytes(video.data))
        if text is not None:
            self._client.sent_text.append(text)

    async def send_client_content(self, *, turns=None, turn_complete=True):
        self._client.client_content.append((turns, turn_complete))

    async def send_tool_response(self, *, function_responses):
        self._client.tool_responses.append(list(function_responses))


class FakeLiveClient:
    """Stands in for ``client`` — only ``client.aio.live.connect`` is used."""

    def __init__(self, turns=(), *, fail_times: int = 0, fail_exc: Exception | None = None):
        self.turns = [list(t) for t in turns]
        self.fail_times = fail_times
        self.fail_exc = fail_exc or RuntimeError("connect refused")
        self.connects: list[dict] = []
        self.sent_audio: list[bytes] = []
        self.sent_video: list[bytes] = []
        self.sent_text: list[str] = []
        self.client_content: list = []
        self.tool_responses: list = []
        self.current: FakeSession | None = None
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

    @asynccontextmanager
    async def _connect(self, *, model, config):
        self.connects.append({"model": model, "config": config})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exc
        session = FakeSession(self)
        self.current = session
        try:
            yield session
        finally:
            self.current = None

    def push_turn(self, messages):
        self.turns.append(list(messages))
        if self.current is not None:
            self.current.close_now()        # let the idle receive() return so the turn is read

    def close_current(self):
        if self.current is not None:
            self.current.close_now()
