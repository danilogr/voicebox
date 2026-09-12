"""Serial speech queue: one child process at a time, never blocking the caller."""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from speak.engines.base import Utterance

logger = logging.getLogger(__name__)

# How long `drain` tolerates nothing at all finishing. Not a budget for the queue: a
# spoken answer is several utterances and each costs its synthesis as well as its audio,
# so a batch runs for minutes. This is the window that says the worker is wedged.
STALL_TIMEOUT = 60.0


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
        self._idle = asyncio.Event()
        self._idle.set()
        self._current: Process | None = None
        self._in_flight = False
        self._finished = 0
        self._generation = 0

    @staticmethod
    def _log_error(error: BaseException) -> None:
        logger.warning("playback failed: %s", error)

    @property
    def size(self) -> int:
        return len(self._waiting) + (1 if self._in_flight else 0)

    def enqueue(self, text: str, *, voice: str | None, rate: float | None) -> int:
        """Queue one utterance and return how many are ahead of it."""
        ahead = self.size
        self._waiting.append(_Pending(text=text, voice=voice, rate=rate))
        self._idle.clear()
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
        if self.size == 0:
            # An utterance still in flight settles in the worker, which sets _idle then.
            self._idle.set()
        return dropped

    async def drain(self, *, stall_timeout: float = STALL_TIMEOUT) -> None:
        """Wait until nothing is queued, being prepared, or playing.

        Waits as long as the queue keeps making progress, however long that takes. A
        fixed deadline cannot serve this: one spoken answer is several utterances, each
        costing its synthesis as well as its audio, which is minutes of legitimate work.
        Only a stall - nothing finishing inside the window - means the worker is wedged.

        Raises:
            TimeoutError: if no utterance finished within `stall_timeout`.
        """
        while not self._idle.is_set():
            finished = self._finished
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=stall_timeout)
            except TimeoutError:
                if self._finished == finished:
                    raise

    async def run(self) -> None:
        """Drain the queue until cancelled."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._waiting:
                await self._play(self._waiting.popleft())
            # Nothing waiting, preparing, or playing: release drain() waiters.
            self._idle.set()

    async def _play(self, pending: _Pending) -> None:
        """Play one utterance, counted by `size` for the whole of its life."""
        self._in_flight = True
        try:
            await self._synthesize_and_play(pending)
        finally:
            self._in_flight = False
            # Counted whether it played, failed or was discarded: `drain` only needs to
            # know the worker is moving, not that the audio was heard.
            self._finished += 1

    async def _synthesize_and_play(self, pending: _Pending) -> None:
        generation = self._generation
        try:
            utterance = await self._prepare(pending.text, voice=pending.voice, rate=pending.rate)
        except Exception as error:
            self._on_error(error)
            return

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
        if generation != self._generation:
            # stop() ran while the player was starting; kill it before it is heard.
            process.terminate()
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
