"""Speech queue: one player at a time, with the next utterance prepared while one plays."""

import asyncio
import logging
import time
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


class Speaker(Protocol):
    """The cross-process claim on the machine's speakers, held around each playback."""

    async def acquire(self, stopped: asyncio.Event) -> bool: ...

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UtteranceOutcome:
    """One utterance's timings and result, in whatever detail the queue could measure.

    Handed to `on_finish` exactly once, at the same point `_finished` is
    incremented, so a telemetry consumer sees every utterance that left the queue
    and none that did not. The queue does not know which engine produced an
    utterance and does not decide it: `engine` and `voice` are what the prepared
    `Utterance` reported about itself, passed through, and fall back to `None` and
    the requested voice when it reported nothing or was never prepared.
    """

    text: str
    voice: str | None
    intent: str | None
    # "ok" played to the end; "interrupted" a `stop()` cut it off while it was
    # audible; "discarded" dropped before anyone heard it - still waiting, still
    # being prepared, or killed while its player was starting; "failed" synthesis
    # failed, the player would not start, or it exited non-zero on its own.
    status: str
    # From `enqueue` until synthesis started - the time spent behind other
    # utterances - or until it was dropped, for one that never reached synthesis.
    # Never includes this utterance's own synthesis or playback.
    queue_wait_ms: int
    synth_ms: int | None
    play_ms: int | None
    audio_ms: int | None
    engine: str | None = None


OnFinish = Callable[[UtteranceOutcome], None]


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


@dataclass(frozen=True, slots=True)
class _Pending:
    text: str
    voice: str | None
    rate: float | None
    intent: str | None
    enqueued_at: float


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One prepared attempt: the utterance (or `None`, meaning prepare failed), the
    generation it was prepared under, the pending request it came from, how long it
    waited before preparing began, and how long preparing it took."""

    utterance: Utterance | None
    generation: int
    pending: _Pending
    queue_wait_ms: int
    synth_ms: int | None


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

    @property
    def size(self) -> int:
        """Utterances that have not left the queue, at whatever stage.

        The one definition of busy. `_idle` derives from it, and so does every caller
        deciding whether to wait, so a stage added to this class has to be added here
        too or it goes uncounted.
        """
        return self.waiting + self.preparing + self.prepared + int(self.playing)


def describe(stats: QueueStats) -> str:
    """What the queue is holding, most advanced stage first.

    One wording, shared by `speech_status` and by the refusal `listen` raises, so the two
    never describe the same queue differently.
    """
    held = []
    if stats.playing:
        held.append("one playing")
    if stats.prepared:
        held.append(f"{stats.prepared} prepared")
    if stats.preparing:
        held.append(f"{stats.preparing} being prepared")
    if stats.waiting:
        held.append(f"{stats.waiting} waiting")
    return ", ".join(held) if held else "nothing in hand"


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

    Every utterance in flight is held by a reference, never by a tally: `_ahead` while it
    is being prepared, `_inflight` from the moment the worker takes it until its temp
    file is gone. `size` reads those references, so no path can drop an utterance from
    the count by forgetting to decrement - including a prefetch cancelled before its
    task ever runs, whose `finally` would never fire.

    With a `speaker`, each utterance waits for the machine's speakers before its player
    starts, and a `stop()` ends that wait at once.
    """

    def __init__(
        self,
        *,
        prepare: Prepare,
        spawn: Spawn = spawn_detached,
        on_error: OnError | None = None,
        on_finish: OnFinish | None = None,
        speaker: Speaker | None = None,
    ) -> None:
        self._prepare = prepare
        self._spawn = spawn
        self._on_error = on_error or self._log_error
        self._on_finish = on_finish
        self._speaker = speaker
        self._stopped = asyncio.Event()
        self._waiting: deque[_Pending] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._player: Process | None = None
        self._ahead: asyncio.Task[_Prepared] | None = None
        self._inflight: _Prepared | None = None
        self._playing = False
        self._finished = 0
        self._generation = 0

    @staticmethod
    def _log_error(error: BaseException) -> None:
        logger.warning("playback failed: %s", error)

    def _finish(self, outcome: UtteranceOutcome) -> None:
        """Report one utterance's outcome. A telemetry failure must never take
        playback down with it, so it is caught here and only logged."""
        if self._on_finish is None:
            return
        try:
            self._on_finish(outcome)
        except Exception:
            logger.debug("telemetry callback failed", exc_info=True)

    def _outcome(self, prepared: _Prepared, status: str, play_ms: int | None) -> UtteranceOutcome:
        pending = prepared.pending
        utterance = prepared.utterance
        engine, voice = None, pending.voice
        if utterance is not None and utterance.engine is not None:
            engine, voice = utterance.engine, utterance.voice
        return UtteranceOutcome(
            text=pending.text,
            voice=voice,
            engine=engine,
            intent=pending.intent,
            status=status,
            queue_wait_ms=prepared.queue_wait_ms,
            synth_ms=prepared.synth_ms,
            play_ms=play_ms,
            audio_ms=utterance.audio_ms if utterance is not None else None,
        )

    @property
    def size(self) -> int:
        return self.stats().size

    def stats(self) -> QueueStats:
        """A snapshot of every stage, for a caller deciding whether to wait."""
        ahead = self._ahead
        return QueueStats(
            waiting=len(self._waiting),
            preparing=int(ahead is not None and not ahead.done()),
            # Everything synthesized but not currently audible: a prefetch, an
            # utterance the worker holds before spawning its player, one whose file is
            # still being deleted, and a synthesis that failed and is on its way out.
            # `size` stays exact; the label is a summary, so it can read 2 at depth one.
            prepared=int(ahead is not None and ahead.done())
            + int(self._inflight is not None and not self._playing),
            playing=self._playing,
            finished=self._finished,
        )

    def enqueue(
        self, text: str, *, voice: str | None, rate: float | None, intent: str | None = None
    ) -> int:
        """Queue one utterance and return how many are ahead of it."""
        ahead = self.size
        self._waiting.append(
            _Pending(text=text, voice=voice, rate=rate, intent=intent, enqueued_at=time.monotonic())
        )
        self._idle.clear()
        self._wake.set()
        return ahead

    async def stop(self) -> int:
        """Silence everything and return how many utterances were dropped."""
        dropped = self.size
        self._generation += 1
        # Ends any wait for the speakers at once; the next wait gets a fresh event.
        self._stopped.set()
        self._stopped = asyncio.Event()
        waiting, self._waiting = list(self._waiting), deque()
        for pending in waiting:
            self._finish(
                UtteranceOutcome(
                    text=pending.text,
                    voice=pending.voice,
                    intent=pending.intent,
                    status="discarded",
                    queue_wait_ms=_ms_since(pending.enqueued_at),
                    synth_ms=None,
                    play_ms=None,
                    audio_ms=None,
                )
            )
        player, self._player = self._player, None
        if player is not None:
            player.terminate()
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
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._waiting or self._ahead is not None:
                    if self._ahead is None:
                        self._ahead = self._start_preparing(self._waiting.popleft())
                    prepared = await self._ahead
                    # Handed straight from one reference to the other, so the utterance
                    # stays counted across the swap.
                    self._ahead, self._inflight = None, prepared
                    if self._waiting:
                        self._ahead = self._start_preparing(self._waiting.popleft())
                    await self._play(prepared)
                self._idle.set()
        finally:
            # Cancelling this task (session teardown) would otherwise abandon `_ahead`
            # mid-flight: it keeps running detached, holding a temp file nobody plays or
            # cleans up.
            await self._discard_prefetch()

    def _start_preparing(self, pending: _Pending) -> asyncio.Task[_Prepared]:
        """Begin synthesizing one utterance, under the generation current right now.

        Sampled here, synchronously with the pop that took `pending` off `_waiting`,
        rather than inside the task: a `stop()` can land before the task takes its first
        step, and by then `self._generation` has moved on. Reading it late would make a
        discarded utterance look current and let it play.
        """
        return asyncio.create_task(self._prepare_one(pending, self._generation))

    async def _prepare_one(self, pending: _Pending, generation: int) -> _Prepared:
        """Synthesize one utterance. `utterance` is `None` when it failed; either way
        the attempt and its timing are reported, so a caller downstream can tell a
        failed prepare from one that never got the chance.

        The wait ends here, when synthesis begins, not when the task was created: with
        a prefetch in hand the next utterance starts preparing while the previous one
        is still playing, so its wait is only the time spent behind earlier syntheses.
        """
        started = time.monotonic()
        waited = round((started - pending.enqueued_at) * 1000)
        utterance: Utterance | None
        try:
            utterance = await self._prepare(pending.text, voice=pending.voice, rate=pending.rate)
        except Exception as error:
            self._on_error(error)
            utterance = None
        return _Prepared(
            utterance=utterance,
            generation=generation,
            pending=pending,
            queue_wait_ms=waited,
            synth_ms=_ms_since(started),
        )

    async def _discard_prefetch(self) -> None:
        """Drop an utterance prepared ahead, and the temp file it is holding."""
        ahead, self._ahead = self._ahead, None
        if ahead is None:
            return
        ahead.cancel()
        try:
            prepared = await ahead
        except asyncio.CancelledError:
            if not ahead.cancelled():
                # The cancel was aimed at this worker, not at `ahead`; let it through.
                raise
            return
        if prepared.utterance is not None:
            await self._cleanup(prepared.utterance)
        status = "failed" if prepared.utterance is None else "discarded"
        self._finish(self._outcome(prepared, status, None))

    async def _play(self, prepared: _Prepared) -> None:
        """Play one prepared utterance, or account for one that never got that far.

        Its temp file is released here and nowhere else: played, discarded by a `stop`,
        or abandoned because the player would not start. Cleanup is itself an `await`, so
        a worker cancelled inside it can still leave the file behind - but the utterance
        leaves the count either way, or a dead queue would report work it will never do.
        """
        status = "ok"
        play_ms: int | None = None
        try:
            if prepared.utterance is None:
                status = "failed"
                return
            if prepared.generation != self._generation:
                # stop() ran while this was being prepared; it is discarded rather than
                # played late.
                status = "discarded"
                return
            if not await self._claim_speaker(prepared.generation):
                # stop() ran while this waited for another process to finish speaking.
                status = "discarded"
                return
            try:
                self._playing = True
                try:
                    status, play_ms = await self._spawn_and_wait(
                        prepared.utterance, prepared.generation
                    )
                finally:
                    self._playing = False
            finally:
                if self._speaker is not None:
                    self._speaker.release()
        finally:
            try:
                if prepared.utterance is not None:
                    await self._cleanup(prepared.utterance)
            finally:
                self._inflight = None
                self._finished += 1
                self._finish(self._outcome(prepared, status, play_ms))

    async def _claim_speaker(self, generation: int) -> bool:
        """Wait for the machine's speakers, unless a `stop()` makes this moot first.

        The stop event is taken before the generation is compared, so a `stop()`
        landing between the two is caught by one or the other.
        """
        if self._speaker is None:
            return True
        stopped = self._stopped
        if generation != self._generation:
            return False
        held = await self._speaker.acquire(stopped)
        if held and generation != self._generation:
            self._speaker.release()
            return False
        return held

    async def _spawn_and_wait(
        self, utterance: Utterance, generation: int
    ) -> tuple[str, int | None]:
        """Start the player and wait for it to exit, killing it if a `stop` lands first.

        Returns `(status, play_ms)`. `play_ms` is `None` only when the process never
        started at all. The generation is checked twice: a `stop()` that landed while
        the player was starting means nobody heard it (`discarded`); one that landed
        while it was playing cut it off (`interrupted`). Checked before the exit code,
        because a player that `stop()` terminated exits non-zero and that is not a
        failure.
        """
        started = time.monotonic()
        try:
            player = await self._spawn(utterance.command, utterance.args)
        except OSError as error:
            self._on_error(error)
            return "failed", None

        self._player = player
        stopped_while_starting = generation != self._generation
        if stopped_while_starting:
            # stop() ran while the player was starting; kill it before it is heard.
            player.terminate()
        try:
            returncode = await player.wait()
        finally:
            self._player = None
        play_ms = _ms_since(started)
        if stopped_while_starting:
            return "discarded", play_ms
        if generation != self._generation:
            return "interrupted", play_ms
        if returncode != 0:
            self._on_error(OSError(f"{utterance.command} exited with status {returncode}"))
            return "failed", play_ms
        return "ok", play_ms

    async def _cleanup(self, utterance: Utterance) -> None:
        if utterance.cleanup is None:
            return
        try:
            await utterance.cleanup()
        except OSError as error:
            logger.debug("cleanup failed: %s", error)
