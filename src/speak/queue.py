"""Serial speech queue: one child process at a time, never blocking the caller."""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from speak.engines.base import Utterance

logger = logging.getLogger(__name__)


class Process(Protocol):
    """The slice of a child process the queue uses."""

    def terminate(self) -> None: ...

    async def wait(self) -> int: ...


Prepare = Callable[..., Awaitable[Utterance]]
Spawn = Callable[[str, Sequence[str]], Awaitable[Process]]
OnError = Callable[[BaseException], None]


@dataclass(frozen=True, slots=True)
class _Pending:
    text: str
    voice: str | None
    rate: float | None


async def spawn_detached(command: str, args: Sequence[str]) -> Process:
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class SpeechQueue:
    """Plays queued utterances one after another.

    `enqueue` is synchronous and returns immediately so the agent never waits on
    playback. A single worker task, started by `run`, drains the queue.
    """

    def __init__(
        self,
        *,
        prepare: Prepare,
        spawn: Spawn = spawn_detached,
        on_error: OnError | None = None,
    ) -> None:
        self._prepare = prepare
        self._spawn = spawn
        self._on_error = on_error or self._log_error
        self._waiting: deque[_Pending] = deque()
        self._wake = asyncio.Event()
        self._current: Process | None = None
        self._preparing = False
        self._generation = 0

    @staticmethod
    def _log_error(error: BaseException) -> None:
        logger.warning("playback failed: %s", error)

    @property
    def size(self) -> int:
        in_flight = self._current is not None or self._preparing
        return len(self._waiting) + (1 if in_flight else 0)

    def enqueue(self, text: str, *, voice: str | None, rate: float | None) -> int:
        """Queue one utterance and return how many are ahead of it."""
        ahead = self.size
        self._waiting.append(_Pending(text=text, voice=voice, rate=rate))
        self._wake.set()
        return ahead

    async def stop(self) -> int:
        """Silence everything and return how many utterances were dropped."""
        dropped = self.size
        self._generation += 1
        self._waiting.clear()
        current, self._current = self._current, None
        if current is not None:
            current.terminate()
        return dropped

    async def run(self) -> None:
        """Drain the queue until cancelled."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._waiting:
                await self._play(self._waiting.popleft())

    async def _play(self, pending: _Pending) -> None:
        generation = self._generation
        self._preparing = True
        try:
            utterance = await self._prepare(pending.text, voice=pending.voice, rate=pending.rate)
        except Exception as error:
            self._on_error(error)
            return
        finally:
            self._preparing = False

        if generation != self._generation:
            # stop() ran while this was being synthesized; discard the work.
            await self._cleanup(utterance)
            return

        try:
            process = await self._spawn(utterance.command, utterance.args)
        except OSError as error:
            self._on_error(error)
            await self._cleanup(utterance)
            return

        self._current = process
        try:
            await process.wait()
        finally:
            self._current = None
            await self._cleanup(utterance)

    async def _cleanup(self, utterance: Utterance) -> None:
        if utterance.cleanup is None:
            return
        try:
            await utterance.cleanup()
        except OSError as error:
            logger.debug("cleanup failed: %s", error)
