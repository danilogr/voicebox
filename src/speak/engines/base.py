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
