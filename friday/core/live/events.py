"""What a Live session tells its consumer. One frozen value object per thing
that can happen, so the hub, the dashboard and (later) the call agent all react
to the same vocabulary instead of decoding Gemini's wire messages themselves."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class Connected:
    resumed: bool


@dataclass(frozen=True)
class Disconnected:
    reason: str
    will_retry: bool


@dataclass(frozen=True)
class AudioOut:
    pcm: bytes                       # 24 kHz mono PCM16 from the model


@dataclass(frozen=True)
class TextOut:
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    role: str                        # "user" | "assistant"
    final: bool


@dataclass(frozen=True)
class Interrupted:
    pass


@dataclass(frozen=True)
class TurnComplete:
    pass


@dataclass(frozen=True)
class ToolStarted:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolFinished:
    name: str
    output: str
    ms: int
    failed: bool = False


@dataclass(frozen=True)
class GoAway:
    # The API sends a duration string such as "3s", not a number.
    time_left: str | None = None


LiveEvent = Union[Connected, Disconnected, AudioOut, TextOut, Transcript, Interrupted,
                  TurnComplete, ToolStarted, ToolFinished, GoAway]
