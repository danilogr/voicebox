"""The job_status tool, minus the MCP plumbing: one report for every kind of job."""

from pathlib import Path

from voicebox.config import Settings
from voicebox.jobs.desk import Desk, duration, reconcile, release_if_idle
from voicebox.jobs.store import (
    CRASHED,
    DONE,
    EMPTY,
    FAILED,
    Job,
    SynthesisJob,
    TranscriptionJob,
)
from voicebox.synthesize.limits import COLD_START_S, FIRST_REQUEST_CEILING_S, WARMUP_TIMEOUT_S
from voicebox.telemetry.rows import UUID4
from voicebox.transcribe.output import transcript_text

# Below this a job is never called wedged, however short its estimate.
MIN_TIMEOUT_S = 300.0
PREVIEW_CHARS = 300


def job_status(desk: Desk, settings: Settings, job_id: str | None) -> str:
    if job_id is None:
        jobs = [reconcile(desk, job) for job in desk.jobs.recent()]
        release_if_idle(desk)
        if not jobs:
            return "no jobs yet"
        return "\n".join(_summary(job) for job in jobs)
    if not UUID4.fullmatch(job_id):
        # The id becomes a path under the state directory; one that is not a job id could
        # name a file anywhere, and a crashed-looking record there would be acted on.
        return f"no job {job_id}: a job id is the one transcribe or synthesize answered with"
    job = desk.jobs.read(job_id)
    if job is None:
        return f"no job {job_id}"
    described = _describe(desk, settings, reconcile(desk, job))
    release_if_idle(desk)
    return described


def _name(job: Job) -> str:
    return Path(job.source).name if job.source else "pasted text"


def _summary(job: Job) -> str:
    return f"{job.job_id}  {job.kind:13}  {job.state:8}  {_name(job)}"


def _describe(desk: Desk, settings: Settings, job: Job) -> str:
    if job.state in (FAILED, CRASHED):
        detail = job.error or ""
        if isinstance(job, SynthesisJob) and job.state == CRASHED and job.chunks:
            detail += f" (after chunk {job.chunks_done} of {job.chunks})"
        tail = desk.jobs.log_tail(job.job_id)
        return f"{job.state}: {_name(job)}: {detail}" + (f"\n{tail}" if tail else "")
    if isinstance(job, TranscriptionJob):
        return _transcription(desk, settings, job)
    if isinstance(job, SynthesisJob):
        return _synthesis(desk, job)
    return f"{job.state}: {_name(job)}"


def _transcription(desk: Desk, settings: Settings, job: TranscriptionJob) -> str:
    name = _name(job)
    if job.state == DONE:
        preview = _preview(job)
        if preview is None:
            return (
                f"done: {name} -> {job.output} ({job.words} words), "
                "but the transcript is no longer at that path"
            )
        return f"done: {name} -> {job.output} ({job.words} words)\n{preview}"
    if job.state == EMPTY:
        return f"finished, but no speech was detected in {name}; output: {job.output}"
    return _running(desk, job, settings.transcribe_timeout, name)


def _synthesis(desk: Desk, job: SynthesisJob) -> str:
    name = _name(job)
    if job.state == DONE:
        seconds = (job.audio_ms or 0) / 1000
        size = (job.output_bytes or 0) / 1_000_000
        return f"done: {name} -> {job.output} ({duration(seconds)} of audio, {size:.1f} MB)"
    elapsed = desk.clock() - job.created_at
    limit = max(MIN_TIMEOUT_S, 4 * job.eta_s)
    if job.slowest_chars_per_s is None:
        # No request long enough to show a pace has finished: on a slow machine a cold
        # start and the first such request alone may take this long before the worker's
        # own timeouts would end them.
        limit = max(limit, COLD_START_S + WARMUP_TIMEOUT_S + FIRST_REQUEST_CEILING_S)
    if elapsed > limit:
        return (
            f"running: {name} for {duration(elapsed)}, well past the "
            f"{duration(job.eta_s)} estimate - it may be wedged"
        )
    if job.chunks is None:
        return f"running: {name}, preparing the text, {duration(elapsed)} elapsed"
    # What is left to speak at the pace shown so far; never zero while the job runs,
    # since joining and writing the file still follow the last chunk.
    left = max(1.0, (job.chars - job.chars_done) / job.chars_per_s)
    return (
        f"running: {name}, chunk {job.chunks_done} of {job.chunks} done, "
        f"{duration(elapsed)} elapsed, about {duration(left)} left"
    )


def _running(desk: Desk, job: Job, configured: float | None, what: str) -> str:
    elapsed = desk.clock() - job.created_at
    timeout = configured if (configured or 0) > 0 else None
    limit = timeout if timeout is not None else max(MIN_TIMEOUT_S, 4 * job.eta_s)
    if elapsed > limit:
        return (
            f"running: {what} for {duration(elapsed)}, well past the "
            f"{duration(job.eta_s)} estimate - it may be wedged"
        )
    remaining = max(0.0, job.eta_s - elapsed)
    return f"running: {what}, {duration(elapsed)} elapsed, about {duration(remaining)} left"


def _preview(job: TranscriptionJob) -> str | None:
    """The start of a finished transcript, or None when it is no longer there: the file
    is the user's from the moment it is written."""
    try:
        text = transcript_text(Path(job.output), job.format)
    except OSError:
        return None
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
