"""One telemetry row per read-aloud run.

A run starts at the first `read_aloud` after the queue has been idle, and ends when the
queue has been empty for `NARRATION_IDLE_S`, when `stop_reading` is called, or when the
session ends. It lives beside the queue whose outcomes it counts, in the MCP server, so
a narration has no worker and is never `crashed`.

Nothing here may delay or break reading: the queue calls `finished` from its worker,
and every path that records goes through the session's never-raising `record`.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from speak_core.queue import UtteranceOutcome

from voicebox.jobs.store import new_job_id
from voicebox.telemetry.rows import NARRATION, JobFacts, iso, job_row, recorded_voice, wall_ms
from voicebox.telemetry.session import TelemetrySession

logger = logging.getLogger(__name__)

NARRATION_IDLE_S = 30.0
NEMO = "nemo"
SAY = "say"


@dataclass(slots=True)
class _Run:
    job_id: str
    started_at: float
    started_monotonic: float
    server_warm: bool | None = None
    sentences: int = 0
    chars: int = 0
    played: int = 0
    failed: int = 0
    synth_ms: int = 0
    audio_ms: int = 0
    fallback: int = 0


class NarrationTracker:
    def __init__(
        self,
        *,
        telemetry: TelemetrySession | None,
        engine: str,
        model: str | None,
        voice: str | None,
        pending: Callable[[], int],
        warm: Callable[[], Awaitable[bool]] | None = None,
        idle_s: float = NARRATION_IDLE_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._telemetry = telemetry
        self._engine = engine
        self._model = model
        self._voice = recorded_voice(engine, voice)
        self._pending = pending
        self._warm = warm
        self._idle_s = idle_s
        self._clock = clock
        self._monotonic = monotonic
        self._run: _Run | None = None
        self._timer: asyncio.TimerHandle | None = None

    async def queued(self, text: str) -> None:
        """One sentence accepted by `read_aloud`, before it is enqueued."""
        self._cancel_timer()
        run = self._run
        if run is None:
            # Opened before awaiting anything, so two sentences arriving together share it.
            run = self._run = _Run(
                job_id=new_job_id(), started_at=self._clock(), started_monotonic=self._monotonic()
            )
            if self._warm is not None:
                run.server_warm = await self._warm()
        run.sentences += 1
        run.chars += len(text)

    def finished(self, outcome: UtteranceOutcome) -> None:
        """The queue's `on_finish`: one sentence left the queue, however it ended."""
        run = self._run
        if run is None:
            return
        if outcome.synth_ms is not None:
            run.synth_ms += outcome.synth_ms
        if outcome.status == "ok":
            run.played += 1
            # A sentence the engine spoke without producing a file has no audio length of
            # its own; how long its player ran is the closest measure.
            run.audio_ms += (
                outcome.audio_ms if outcome.audio_ms is not None else outcome.play_ms or 0
            )
            if self._engine == NEMO and outcome.engine == SAY:
                run.fallback += 1
        elif outcome.status == "interrupted":
            run.audio_ms += outcome.play_ms or 0
        elif outcome.status == "failed":
            run.failed += 1
        if self._pending() == 0:
            self._arm_timer()

    def refused(self, reason: str) -> None:
        """A `read_aloud` turned away. Recorded as a run of its own only when none is open:
        inside a reading it is one sentence the model will retry."""
        if self._run is not None:
            return
        now, now_monotonic = self._clock(), self._monotonic()
        self._record(
            _Run(job_id=new_job_id(), started_at=now, started_monotonic=now_monotonic),
            "refused",
            reason,
        )

    def stopped(self) -> None:
        """`stop_reading` ran: the run ends here."""
        self._close(idle=False)

    def close(self) -> None:
        """The session is ending: an open run is recorded now or never."""
        self._close(idle=False)

    def _arm_timer(self) -> None:
        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = loop.call_later(self._idle_s, self._on_idle)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_idle(self) -> None:
        self._timer = None
        if self._pending() == 0:
            self._close(idle=True)

    def _close(self, *, idle: bool) -> None:
        self._cancel_timer()
        run, self._run = self._run, None
        if run is None:
            return
        if idle and run.played == 0 and run.failed > 0:
            self._record(run, "failed", "other")
        elif idle or run.played >= run.sentences:
            self._record(run, "ok", None)
        else:
            self._record(run, "stopped", None)

    def _record(self, run: _Run, status: str, reason: str | None) -> None:
        telemetry = self._telemetry
        if telemetry is None:
            return
        try:
            row = self._row(telemetry, run, status, reason)
        except ValueError:
            logger.debug("telemetry: could not build a narration row", exc_info=True)
            return
        telemetry.record(row)

    def _row(
        self, telemetry: TelemetrySession, run: _Run, status: str, reason: str | None
    ) -> dict[str, object]:
        now = self._clock()
        facts = JobFacts(
            job_id=run.job_id,
            session_id=telemetry.session_id,
            install_id=telemetry.install_id,
            kind=NARRATION,
            started_at=iso(run.started_at),
            ended_at=iso(now),
            wall_ms=wall_ms(
                run.started_at, run.started_monotonic, now=now, now_monotonic=self._monotonic()
            ),
            status=status,
            reason=reason,
            stage="play",
            engine=self._engine,
            model=self._model,
        )
        return job_row(
            facts,
            {
                "server_warm": run.server_warm,
                "sentences": run.sentences,
                "sentences_played": run.played,
                "chars": run.chars,
                "voice": self._voice,
                "synth_ms": run.synth_ms,
                "audio_ms": run.audio_ms,
                "fallback_sentences": run.fallback,
            },
        )
