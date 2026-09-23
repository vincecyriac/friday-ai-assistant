# Sub-project 3: Live engine extraction and browser voice

**Roadmap:** `docs/superpowers/specs/2026-09-21-sentinel-evolution-roadmap.md` (sub-project 3)
**Builds on:** sub-project 1 (`…-vault-and-dashboard-auth-design.md`) and sub-project 2
(`…-dashboard-ui-and-assistant-design.md`), both implemented.

## 1. Goal

Move the Gemini Live session out of `friday/desktop/hub.py` into `friday.core.live`, where it
becomes a provider-neutral-shaped, tested component with an event stream and a pluggable audio
transport; make `hub.py` its first consumer with behaviour unchanged; and give the dashboard a
voice assistant — the sentinel running its own Live session, the FRIDAY orb, and a mic button —
using the same seven tools the text assistant already has. The call agent in sub-project 6 then
drives the same `LiveSession` over ALSA instead of a WebSocket.

### Decisions (from brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Who hosts dashboard voice | The sentinel runs its own Live session | Works with the Mac asleep; the key never leaves the vault; it is exactly what sub-project 6 needs |
| Extraction scope | Session + transports + event stream; tools/senses/UI stay in the hub | The hard-won loop lives once; core gains no vision abstraction it has no use for |
| Voice tools | The same seven as text chat (read six + `set_controls`) | One ToolSet, one audit trail; macOS actions stay on the desktop's own session |
| Orb | Port the desktop's Three.js orb | FRIDAY looks the same on both screens |
| Orb assets | Shared `friday/webassets/`, mounted by both nodes | One three.js, one orb.js; a fix lands on both |
| Mic control | Explicit session, hands-free with barge-in inside it | A tab may not hold the mic unasked; a phone may not hold it open |
| Browser audio transport | Dedicated `/voice/ws`, binary frames | No base64 tax; audio does not flow through the shared event socket |
| Transport coupling | Event stream is the contract, transport optional | The hub keeps its mic/playback multiplexing instead of inverting it |

### Non-goals

- Changing what the desktop assistant can do. The hub's 38 tools, widgets, senses, status
  wiring, remote-phone mirroring and session-handle-on-disk all stay exactly as they are.
- Voice control of macOS from the dashboard. A relay tool needs a request/response path the
  bus does not have; it is its own sub-project.
- A second LLM provider behind the Live session. Gemini Live is a provider-specific protocol;
  `friday.core.llm`'s text protocol is unaffected.
- Wake words, speaker diarisation, or always-on server-side listening.
- Telephony. Sub-project 6 adds `AlsaTransport` and `CallSession` on top of what this builds.

## 2. What moves and what stays

Today `hub.py` (2,552 lines) spreads the session across `run_friday`'s connect/resume loop
(≈75 lines), `run_session_tasks` (≈25), `receive_audio_task` (≈115), `send_audio_task` (≈30) and
parts of `stream_senses_task`.

**Moves into `friday.core.live`** (≈330 lines, once):

- the connect → run → rotate loop, its backoff, and the two handle rules: a handle is dropped
  only when the handshake itself was refused while resuming, and kept when an established
  session drops;
- the outer `while` around `session.receive()` — without it the task falls through after one
  turn, the session is torn down, and the resumed handle replays that turn's tool calls;
- decoding every server message into typed events: interruption, model audio and text,
  `turn_complete`, `session_resumption_update`, `go_away`, `tool_call`;
- tool dispatch: call the injected handler, feed an optional returned image back as
  `send_realtime_input(video=…)`, and **always** answer with `send_tool_response` — a raising
  tool that is skipped leaves the turn open forever and re-fires on every later resume;
- `send_realtime_input` wrappers for audio, video and text.

**Stays in `hub.py`**: PyAudio streams and `CHUNK_SIZE`; `play_queue` with its
remote-client mirroring and interrupt drain; `stream_senses_task` (screen/webcam capture and
the phone-camera precedence rules); `execute_tool` and all 38 declarations; widgets; status and
`broadcast_event`; `save_session_handle`/`clear_session_handle` on disk; `build_system_instruction`.

After the refactor `hub.py` builds a `LiveSession`, pushes mic bytes into it, and reacts to its
events. Net: ~330 lines out, ~120 lines of wiring in.

## 3. `friday.core.live`

```
friday/core/live/
├── __init__.py    re-exports LiveConfig, LiveSession, AudioTransport and every event
├── events.py      the event dataclasses
├── transport.py   AudioTransport protocol + QueueTransport
└── session.py     LiveConfig, LiveSession
```

Dependencies: `google-genai` only (already a base dependency, already imported by
`friday/core/llm/gemini.py`). `tests/test_boundaries.py` must stay green — no PyAudio, no ALSA,
no aiohttp-specific types in core.

### 3.1 Events (`events.py`)

All frozen dataclasses; `LiveEvent` is their union.

| Event | Fields | Emitted when |
|---|---|---|
| `Connected` | `resumed: bool` | the session handshake succeeded |
| `Disconnected` | `reason: str`, `will_retry: bool` | the session ended or the connect failed |
| `AudioOut` | `pcm: bytes` | a model audio part arrives (24 kHz PCM) |
| `TextOut` | `text: str` | a model text part arrives |
| `Transcript` | `text: str`, `role: str` (`"user"`/`"assistant"`), `final: bool` | input/output transcription, when enabled |
| `Interrupted` | — | `server_content.interrupted` (barge-in) |
| `TurnComplete` | — | `server_content.turn_complete` |
| `ToolStarted` | `name: str`, `args: dict` | before the handler runs |
| `ToolFinished` | `name: str`, `output: str`, `ms: int`, `failed: bool` | after it runs (or raised) |
| `GoAway` | `time_left: str \| None` | the server asks for rotation (a duration string, e.g. `"3s"`) |

`ToolStarted`/`ToolFinished` carry the same shape the dashboard's NDJSON chat already uses, so
one accordion renders both engines.

### 3.2 Transport (`transport.py`)

```python
class AudioTransport(Protocol):
    async def start(self) -> None: ...
    async def read(self) -> bytes | None: ...     # 16 kHz mic PCM; None ends the pump
    async def write(self, pcm: bytes) -> None: ...  # 24 kHz model PCM
    async def clear(self) -> None: ...            # drop buffered playback on barge-in
    async def stop(self) -> None: ...
```

`QueueTransport` is a concrete in-memory implementation used by tests and by any caller that
wants to push and pull through queues. `WebSocketTransport` lives in `friday.sentinel.voice`;
`PyAudioTransport` is **not** written — the hub keeps its own plumbing and passes no transport.

### 3.3 `LiveSession` (`session.py`)

```python
@dataclass(frozen=True)
class LiveConfig:
    model: str
    voice: str = "Aoede"
    system_instruction: str = ""
    tools: tuple[Mapping[str, Any], ...] = ()       # JSON-schema function declarations
    google_search: bool = False
    input_transcription: bool = False
    output_transcription: bool = False
    resume_handle: str | None = None
    max_backoff_s: float = 10.0

ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bytes | None]]]

class LiveSession:
    def __init__(self, client, config: LiveConfig, *, tool_handler: ToolHandler | None = None,
                 transport: AudioTransport | None = None,
                 on_handle: Callable[[str], None] | None = None,
                 logger: logging.Logger | None = None) -> None
    @property
    def state(self) -> str                 # "idle" | "connecting" | "live" | "reconnecting" | "closed"
    async def run(self) -> None            # connect/rotate loop until stop(); never raises for a connect failure
    def stop(self) -> None
    async def send_audio(self, pcm: bytes) -> None
    async def send_video(self, jpeg: bytes) -> None
    async def send_text(self, text: str, *, turn_complete: bool = True) -> None
    def events(self) -> AsyncIterator[LiveEvent]
```

Behaviour:

- `run()` loops until `stop()`: build `LiveConnectConfig` (audio modality, voice, system
  instruction, tools + optional `GoogleSearch`, `SessionResumptionConfig(handle=…)`, the two
  `AudioTranscriptionConfig`s when enabled), `async with client.aio.live.connect(...)`, emit
  `Connected`, run the receive loop (and the transport pumps if a transport was given), emit
  `Disconnected`, then rotate. `transparent=` is never set: it is Vertex-only and the Developer
  API refuses the whole connection when present.
- Backoff on failure: `min(max_backoff_s, 2 ** min(failures, 3))`, reset on success.
- `on_handle(new_handle)` fires for every resumable `session_resumption_update`; the hub writes
  it to disk, the sentinel keeps it in memory.
- `send_*` are no-ops (logged at debug) when no session is live, so a caller never has to guard.
- `events()` yields from an unbounded queue owned by the session; one consumer is expected.
  Events are dropped, never blocked on, if no one is consuming.
- Tool calls: emit `ToolStarted`, run `tool_handler` (any exception becomes
  `"[Tool error] <name> failed: …"`), send the image back as video when one is returned, emit
  `ToolFinished`, then `send_tool_response` for every call in the batch. With no handler set,
  the session answers `"[no tool handler configured]"` so the turn still closes.

## 4. The hub as a consumer

`run_friday` keeps its PyAudio setup and its worker tasks. The connect loop, `run_session_tasks`,
`send_audio_task`'s Gemini half and `receive_audio_task` are replaced by:

- a `LiveSession` built from `LiveConfig(model=MODEL_ID, voice=LIVE_VOICE,
  system_instruction=system_instruction_text, tools=TOOL_FUNCTION_DECLARATIONS,
  google_search=True, resume_handle=current_session_handle)`, `tool_handler=execute_tool`,
  `on_handle=save_session_handle`, no transport;
- `mic_pump_task`: what `send_audio_task` already is, with the `session.send_realtime_input`
  call replaced by `session.send_audio(data)` — the buffering and the
  "only when no WS client is attached" rule are unchanged;
- `live_event_task`: consumes `session.events()` and performs today's reactions —
  `AudioOut` → `play_queue.put` + `broadcast_event("audio_out")` + status `Speaking`;
  `TextOut` → chat log broadcast; `Interrupted` → drain `play_queue`, `interrupted_event`,
  `broadcast_event("interrupted")`, status; `TurnComplete` → status `Listening`;
  `ToolStarted`/`ToolFinished` → the two `tool_activity` broadcasts;
  `Connected`/`Disconnected` → the existing status and log lines;
- `stream_senses_task` unchanged except `session.send_realtime_input(video=…)` →
  `session.send_video(...)`; the same for the WS handler's `remote_camera_frame`, `audio_in`
  and `user_text` branches, which call `send_video` / `send_audio` / `send_text` on the module
  -level session instead of `global_live_session`.
- `global_live_session` remains (other call sites reference it) but now holds the `LiveSession`.

`clear_session_handle()` at boot and the on-disk handle helpers stay in `hub.py`.

## 5. Sentinel voice

### 5.1 `friday/sentinel/voice.py`

- `WebSocketTransport(ws)` — `read()` awaits the next binary frame from the aiohttp
  `WebSocketResponse`; `write(pcm)` sends a binary frame; `clear()` sends
  `{"type": "clear"}` so the browser drops queued playback on barge-in; `stop()` closes.
- `VoiceSession(services, user, conversation_id, ws)` — owns a `LiveSession` with
  `LiveConfig(model=parse_route(config.get("llm.routes.live")).model,
  voice=config.get("voice.name"), system_instruction=assistant.SYSTEM_PROMPT.format(...),
  tools=ToolSet.declarations, input_transcription=True, output_transcription=True)`,
  `tool_handler` wrapping `ToolSet.call` to `(text, None)`, and a `WebSocketTransport`.
  It forwards control events to the browser as JSON and persists transcripts (§5.3).
- The google-genai client comes from `gemini_client(apply_overrides(settings, {...}))` — the
  same vault-first key resolution `Services.provider_for` performs, factored into
  `Services.live_client()` so both paths share one cached client. A non-`gemini` provider in
  `llm.routes.live`, or a missing key, closes the socket with
  `{"type": "error", "message": "… Settings → llm"}`.

### 5.2 `GET /voice/ws`

Session cookie or node token (`require_principal`), so the phone can use it over Tailscale.
Refused with 503 `{"error": "voice is disabled"}` when `voice.enabled` is false. One
`LiveSession` per socket; closing the socket stops it. Frames:

| Direction | Frame | Meaning |
|---|---|---|
| in | binary | 16 kHz mono PCM mic chunk |
| in | `{"type":"text","content":"…"}` | typed message injected into the voice turn |
| out | binary | 24 kHz mono PCM model audio |
| out | `{"type":"state","value":"connecting\|live\|reconnecting\|closed"}` | drives the orb and the button |
| out | `{"type":"transcript","role":"user\|assistant","text":"…","final":true}` | live captions |
| out | `{"type":"tool","phase":"start\|done","name":…,"args"\|"output","ms"}` | the accordion |
| out | `{"type":"clear"}` | barge-in: drop queued playback |
| out | `{"type":"error","message":"…"}` | terminal problem; the socket then closes |

A `voice.session` audit entry records start and end (actor `user:<name>`, detail
`{conversation_id, seconds}`), so a voice session shows up in Activity like everything else.

### 5.3 Transcripts in the thread

Storage v4 adds one column: `messages.via TEXT NOT NULL DEFAULT 'text'` (`text` | `voice`).
`Store.message_append` gains `via="text"`; `_message_dict` and the chat API return it.

A final user transcript appends a `user` message with `via="voice"`; a final assistant
transcript appends an `assistant` message with `via="voice"`; tool steps append `tool` rows as
the text loop already does, so one thread interleaves both engines and the dashboard renders
voice turns with a small mic glyph. Interim transcripts are shown live but never stored. The
voice session binds to the conversation that is open in the dashboard; if none is, it creates
one titled from the first final user transcript.

## 6. Shared web assets and the orb

New package `friday/webassets/` (added to `[tool.setuptools.package-data]`), holding
`three.module.min.js` (moved from `friday/desktop/web_gui/vendor/`) and `orb.js` (moved from
`friday/desktop/web_gui/`). Both nodes mount it at `/shared`:
`app.router.add_static("/shared", WEBASSETS_DIR)` in the hub's GUI server and in
`friday/sentinel/web.py`.

- `orb.js` gains `export function mountOrb(element)` and keeps auto-mounting to `#orb-stage`
  when that element exists, so `web_gui/index.html` changes only its importmap
  (`"three": "shared/three.module.min.js"`) and its orb `<script src="shared/orb.js">`.
  `window.FridayOrb.{setState, setLevel, resize}` is unchanged; `sve.js`,
  `asset_viewer.js`, `gestures.js` and `vendor/*` resolve `"three"` through the same importmap.
- The dashboard adds the same importmap and calls `mountOrb(stage)`.
- The orb keeps its own palette (cyan / blue / amber / emerald / ember). It is the one
  deliberate exception to the console's restrained tones: FRIDAY's presence, identical on both
  screens, contained inside its stage.

## 7. Dashboard

The Assistant page gains, above the thread:

- an orb stage (`h-40`, centred) fed by the socket's `state` and by mic amplitude
  (`FridayOrb.setLevel`), plus `speaking` while audio plays;
- a mic button beside Send. Click → `getUserMedia({audio: {channelCount: 1, sampleRate: 16000,
  echoCancellation: true, noiseSuppression: true}})` → open `/voice/ws` → stream 16 kHz PCM
  from an `AudioWorklet` (with a `ScriptProcessor` fallback), exactly as `web_gui/app.js`
  already does. Playback goes through a scheduled `AudioContext` queue at 24 kHz; a `clear`
  frame empties it for barge-in. Click again / navigate away / close the tab → socket closed,
  tracks stopped, orb `idle`.
- live captions under the orb while a turn is in flight; final transcripts drop into the thread
  as ordinary messages with a mic glyph.

Failure modes are visible, never silent: permission denied, no `AudioWorklet`, `voice.enabled`
false, or a missing API key each disable the button with a one-line reason. Text chat is
unaffected in every case.

New dashboard files: `views/voice.js` (socket + audio plumbing, imported by `views/assistant.js`).
`tailwind.css` is rebuilt whenever a class changes, as before.

## 8. Config

| Key | Type | Group | Default | Notes |
|---|---|---|---|---|
| `voice.enabled` | bool | voice | `true` | kill switch: hides the mic button, refuses `/voice/ws` |
| `voice.name` | enum | voice | `Aoede` | `Aoede`, `Kore`, `Charon`, `Fenrir`, `Puck`; the sentinel's own voice |

`GROUP_ORDER` becomes `("llm", "desktop", "sentinel", "voice", "controls")`. The model comes
from the existing `llm.routes.live` — scopes gate what a *node pulls*, not what the sentinel
reads, so no new route key is needed. `desktop.voice` stays as the HUD's voice, so the Mac and
the server can differ.

## 9. Error handling

- Gemini unreachable at socket open: `{"type":"error"}` then close; the button re-enables.
- A drop mid-session: `LiveSession` reconnects with backoff, the browser sees
  `state: reconnecting`, the orb dims, queued playback is cleared. No audio is buffered across
  the gap.
- The browser vanishing: the socket closes, `VoiceSession.stop()` runs, the Live session closes,
  the mic pump ends. Nothing is left running.
- A tool raising: becomes `[Tool error] …` text, the turn closes normally (the core rule).
- `set_controls` by voice goes through the same registry validation and audit as by text; a
  rejected value is spoken back, nothing is written.
- Two voice sockets for one conversation: the second gets 409, matching the text turn lock.

## 10. Testing

- **core/live:** a `FakeLiveClient` scripting server messages gives the hard paths their first
  real tests — multi-turn receive loop; interruption; a tool call answered with
  `send_tool_response`; a raising tool still answered; an image returned by a tool sent as
  video; `go_away` → rotation; resumption handle surfaced through `on_handle`; a refused
  resume handshake clearing the handle while an established drop keeps it; backoff growth;
  `send_*` before connect being no-ops; `stop()` ending `run()` promptly; `QueueTransport`
  pumping both directions and `clear()` on barge-in.
- **hub:** the existing import smoke, plus a test that drives the hub's `live_event_task`
  against scripted events and asserts the reactions (play queue, broadcasts, status).
- **sentinel:** `/voice/ws` auth (cookie, node token, anonymous), the `voice.enabled` gate,
  binary frame → `send_audio`, `AudioOut` → binary frame, transcript persistence with
  `via="voice"`, tool frames, the 409 second socket, audit start/end, and
  `Services.live_client()` raising `ConfigError` for a missing key or a non-Gemini route.
- **storage:** v3 → v4 migration keeps rows; `via` defaults to `text`.
- **dashboard:** existing file/class-coverage and `node --check` extended to `orb.js` and
  `views/voice.js`; the shared mount serves `/shared/three.module.min.js`.
- **boundary:** unchanged and green — `friday.core.live` imports only `google-genai`.

## 11. Risk

`hub.py` is the daily driver and has no session-level tests today. The extraction keeps
behaviour identical and gains the fake-client tests, but the real proof is manual and belongs
at the end of the hub phase, not the end of the project: talk to FRIDAY, interrupt mid-sentence,
fire a tool that returns an image (`look_at_screen`), leave it running long enough to rotate,
and confirm the widget deck and phone mirroring still behave. If any of that regresses, the hub
phase is revertible on its own — the core package it depends on is additive.

## 12. Implementation phases

1. **Core events and session** — `events.py`, `session.py`, `FakeLiveClient`, the full test set.
2. **Transports** — `AudioTransport`, `QueueTransport`, pump loops in `LiveSession`.
3. **Hub delegates** — `hub.py` consumes `LiveSession`; then the manual desktop pass (§11).
4. **Shared web assets** — `friday/webassets/`, `mountOrb`, hub importmap and static route.
5. **Sentinel voice** — schema v4, `voice` registry group, `voice.py`, `/voice/ws`, transcripts.
6. **Dashboard** — orb stage, mic button, `views/voice.js`, captions, thread glyphs.
7. **Docs and verification** — `readme.md`, `deploy/README.md`, full suite, boundary, manual
   pass on desktop and phone widths.
