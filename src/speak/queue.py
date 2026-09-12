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


@dataclass(frozen=True, slots=True)
class _Prepared:
    """An utterance ready to play, and the generation it was prepared under."""

    utterance: Utterance
    generation: int


@dataclass(frozen=True, slots=True)
class QueueStats:
    """What the queue is holding right now.

    Exposed because a slow machine is indistinguishable from a stuck one from the
    outside: a caller that can see three utterances waiting and one being prepared knows
    to wait, where a bare "still busy" leaves it guessing.
    """

    waiting: int
    preparing: int
    prepared: int
    playing: bool
    finished: int


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
        self._preparing = 0
        self._prepared = 0
        self._playing = False
        self._finished = 0
        self._generation = 0
        self._ahead: asyncio.Task[_Prepared | None] | None = None

    @staticmethod
    def _log_error(error: BaseException) -> None:
        logger.warning("playback failed: %s", error)

    @property
    def size(self) -> int:
        """Utterances that have not left the queue, at whatever stage."""
        return len(self._waiting) + self._preparing + self._prepared + (1 if self._playing else 0)

    def stats(self) -> QueueStats:
        """A snapshot of every stage, for a caller deciding whether to wait."""
        return QueueStats(
            waiting=len(self._waiting),
            preparing=self._preparing,
            prepared=self._prepared,
            playing=self._playing,
            finished=self._finished,
        )

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
            # Anything still preparing or playing settles in the worker, which sets
            # _idle when it has nothing left in hand.
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
        """Play queued utterances in order, preparing the next while one plays.

        Depth one, which is what preserves order for free: there is never more than one
        utterance prepared ahead, so the deque alone decides what comes next. Synthesis
        costs about as much wall clock as the audio it produces, so overlapping the two
        nearly removes it from the total.
        """
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._waiting or self._ahead is not None:
                if self._ahead is None:
                    self._ahead = self._start_preparing(self._waiting.popleft())
                prepared = await self._ahead
                self._ahead = None
                if self._waiting:
                    self._ahead = self._start_preparing(self._waiting.popleft())
                await self._play(prepared)
            self._idle.set()

    def _start_preparing(self, pending: _Pending) -> "asyncio.Task[_Prepared | None]":
        self._preparing += 1
        return asyncio.create_task(self._prepare_one(pending))

    async def _prepare_one(self, pending: _Pending) -> _Prepared | None:
        """Synthesize one utterance. None means it failed and was reported."""
        generation = self._generation
        try:
            utterance = await self._prepare(pending.text, voice=pending.voice, rate=pending.rate)
        except Exception as error:
            self._on_error(error)
            return None
        finally:
            self._preparing -= 1
        self._prepared += 1
        return _Prepared(utterance=utterance, generation=generation)

    async def _play(self, prepared: _Prepared | None) -> None:
        """Play one prepared utterance, or account for one that never got that far."""
        try:
            if prepared is None:
                return
            if prepared.generation != self._generation:
                # stop() ran while this was being prepared; discard the work and the
                # temp file that came with it. Counted until that file is gone: `_idle`
                # is derived from `size`.
                try:
                    await self._cleanup(prepared.utterance)
                finally:
                    self._prepared -= 1
                return
            self._prepared -= 1
            self._playing = True
            try:
                await self._spawn_and_wait(prepared.utterance, prepared.generation)
            finally:
                self._playing = False
        finally:
            self._finished += 1

    async def _spawn_and_wait(self, utterance: Utterance, generation: int) -> None:
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
