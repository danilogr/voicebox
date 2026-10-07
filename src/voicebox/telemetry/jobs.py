"""A finished job's telemetry row, built from its final record.

Whoever ends a job records it: the detached worker when it finishes or fails, the tool
when it refuses, and whichever process notices that a worker died. Rows go in
insert-if-absent, so a crash noticed twice is one row.
"""

import logging
import time
from pathlib import Path

from voicebox.jobs.store import (
    CRASHED,
    DONE,
    EMPTY,
    FAILED,
    Job,
    SynthesisJob,
    TranscriptionJob,
)
from voicebox.telemetry.record import insert_at
from voicebox.telemetry.rows import JobFacts, iso, job_row, recorded_voice, wall_ms

logger = logging.getLogger(__name__)

NEMO = "nemo"
STATUS_OF_STATE = {DONE: "ok", EMPTY: "empty", FAILED: "failed", CRASHED: "crashed"}
# The one reason that is only ever a refusal: a worker that lost the lock race never
# started work, whatever state its record ends in.
REFUSED_REASON = "busy"


def transcription_fields(job: TranscriptionJob, *, ran: bool) -> dict[str, object]:
    """`ran` is whether a worker did any work: `converted` says what happened, so a job
    that never started has no answer to give."""
    return {
        "source_bytes": job.size_bytes,
        # Unrounded: `job_row` rounds a finite value and drops one that is not.
        "audio_ms": job.duration_s * 1000,
        "audio_codec": job.codec,
        "has_video": job.has_video,
        "converted": job.convert if ran else None,
        "convert_ms": job.convert_ms,
        "asr_ms": job.asr_ms,
        "diarize": job.diarize,
        "language": job.language or "auto",
        "translate": job.translate_to is not None,
        "format": job.format,
        "words": job.words,
        "chars": job.chars,
    }


def synthesis_fields(job: SynthesisJob) -> dict[str, object]:
    return {
        "server_warm": job.server_warm,
        "input_kind": job.input_kind,
        "input_format": job.input_format,
        "chars": job.chars,
        "words": job.words,
        "paragraphs": job.paragraphs,
        "chunks": job.chunks,
        "chunks_done": job.chunks_done,
        "voice": recorded_voice(NEMO, job.voice),
        "synth_ms": job.synth_ms,
        "encode_ms": job.encode_ms,
        "audio_ms": job.audio_ms,
        "format": job.format,
        "output_bytes": job.output_bytes,
    }


def _kind_fields(job: Job, *, ran: bool) -> tuple[str | None, dict[str, object]]:
    """The model a kind ran and its own fields."""
    if isinstance(job, TranscriptionJob):
        return job.asr_model, transcription_fields(job, ran=ran)
    if isinstance(job, SynthesisJob):
        return job.tts_model, synthesis_fields(job)
    raise TypeError(f"no telemetry fields for a {job.kind} job")


def finished_row(job: Job, *, now: float, now_monotonic: float) -> dict[str, object] | None:
    """The row for a job in a terminal state, or None when its session records nothing."""
    ref = job.telemetry
    if ref is None or job.state not in STATUS_OF_STATE:
        return None
    status = "refused" if job.reason == REFUSED_REASON else STATUS_OF_STATE[job.state]
    # A refusal and a job whose worker never wrote its pid did no work.
    model, fields = _kind_fields(job, ran=status != "refused" and job.worker_pid is not None)
    facts = JobFacts(
        job_id=job.job_id,
        session_id=ref["session_id"],
        install_id=ref["install_id"],
        kind=job.kind,
        started_at=iso(job.created_at),
        ended_at=iso(job.finished_at if job.finished_at is not None else now),
        wall_ms=wall_ms(
            job.created_at, job.created_monotonic, now=now, now_monotonic=now_monotonic
        ),
        status=status,
        reason=job.reason,
        stage=job.stage,
        engine=NEMO,
        model=model,
        error_message=job.error,
        source_name=Path(job.source).name or None,
        output_path=job.output or None,
    )
    return job_row(facts, fields)


def record_job(job: Job) -> None:
    """Record a job that has ended, for the session that asked. Never raises."""
    try:
        row = finished_row(job, now=time.time(), now_monotonic=time.monotonic())
    except (TypeError, ValueError, KeyError):
        logger.debug("telemetry: could not build the row for job %s", job.job_id, exc_info=True)
        return
    if row is not None and job.telemetry is not None:
        insert_at(Path(job.telemetry["db_path"]), row)
