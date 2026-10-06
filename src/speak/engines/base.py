"""The contract every speech engine satisfies."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Utterance:
    """A command to run to make one sentence audible."""

    command: str
    args: Sequence[str]
    cleanup: Callable[[], Awaitable[None]] | None = None
    # The audio's own duration, when an engine can know it cheaply - only `nemo`
    # does, from the WAV it already holds in memory. `None` when an engine speaks
    # without ever producing a file, which is every other engine.
    audio_ms: int | None = None
    # The engine that actually produced this and the voice it spoke in, when that
    # engine says so: a fallback reports itself, not the engine that was asked. A
    # `None` engine means the configured engine, speaking the voice it was asked for.
    engine: str | None = None
    voice: str | None = None


@runtime_checkable
class Engine(Protocol):
    """Builds playable commands and reports the voices it can use."""

    name: str
    default_voice: str | None

    # Voices to draw from when one is chosen at random. None means every voice
    # the engine reports is eligible.
    random_pool: tuple[str, ...] | None

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance: ...

    async def list_voices(self) -> list[str]: ...
