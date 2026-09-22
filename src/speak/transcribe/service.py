"""The transcribe and transcription_status tools, minus the MCP plumbing.

`server.py` defines the tools and calls these; nothing here knows about MCP,
which keeps the transport layer free of transcription logic.
"""

import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from speak.config import Settings
from speak.exclusive import ExclusiveLock
from speak.supervisor import process_is_alive, spawn_server
from speak.telemetry.session import TelemetrySession, TranscriptionOutcome, record_transcription
from speak.transcribe.commands import ToolMissing
from speak.transcribe.jobs import (
    CRASHED,
    DONE,
    EMPTY,
    FAILED,
    RUNNING,
    Job,
    JobStore,
    new_job_id,
)
from speak.transcribe.lock import TranscriptionBusy, transcription_lock
from speak.transcribe.output import FORMATS, output_path, transcript_text
from speak.transcribe.prepare import needs_conversion
from speak.transcribe.probe import Probe, ProbeFailed, probe

# How long a job may sit with no worker pid before it counts as never started.
WORKER_START_GRACE_S = 60.0
# Below this a job is never called wedged, however short its estimate.
MIN_TIMEOUT_S = 300.0
PREVIEW_CHARS = 300


@dataclass(frozen=True)
class Scribe:
    jobs: JobStore
    lock: Callable[[], ExclusiveLock]
    is_alive: Callable[[int], bool]
    spawn: Callable[[Sequence[str]], Awaitable[int]]
    probe: Callable[[Path], Awaitable[Probe]]
    clock: Callable[[], float] = time.time
    pid: int = field(default_factory=os.getpid)


@dataclass(frozen=True)
class TranscribeRequest:
    path: str
    output_dir: str | None
    format: str
    diarize: bool
    language: str | None
    translate_to: str | None


def build_scribe(settings: Settings) -> Scribe:
    return Scribe(
        jobs=JobStore(settings.state_dir),
        lock=lambda: transcription_lock(settings.state_dir, is_alive=process_is_alive),
        is_alive=process_is_alive,
        # Detached, like the speech server: the job must outlive this session.
        spawn=lambda argv: spawn_server(argv[0], argv[1:]),
        probe=probe,
    )


async def start(
    scribe: Scribe,
    settings: Settings,
    request: TranscribeRequest,
    telemetry: TelemetrySession | None,
) -> str:
    source = Path(request.path).expanduser()
    if not source.is_file():
        return f"no such file: {source}"
    if request.format not in FORMATS:
        return f"unknown format {request.format!r}; use one of {', '.join(FORMATS)}"
    try:
        found = await scribe.probe(source)
    except ToolMissing as error:
        return str(error)
    except ProbeFailed as error:
        return f"could not read {source.name}: {error}"
    if not found.has_audio:
        _refused(telemetry, settings, request, found, audio=False)
        return (
            f"{source.name} has no audio stream - it is video only, "
            "so there is nothing to transcribe"
        )

    directory = _destination(request.output_dir, settings.transcribe_dir, source)
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        return f"cannot write transcripts to {directory}"

    lock = scribe.lock()
    try:
        lock.acquire()
    except TranscriptionBusy:
        _refused(telemetry, settings, request, found, audio=True)
        return _busy(scribe)

    job_id = new_job_id()
    job = Job(
        job_id=job_id,
        state=RUNNING,
        source=str(source.resolve()),
        output=str(output_path(source, directory, request.format, job_id)),
        format=request.format,
        diarize=request.diarize,
        language=request.language,
        translate_to=request.translate_to,
        duration_s=found.duration_s,
        size_bytes=found.size_bytes,
        codec=found.codec,
        convert=needs_conversion(found),
        asr_model=settings.nemo_asr_model,
        nemo_binary=settings.nemo_binary,
        claimed_by=scribe.pid,
        created_at=scribe.clock(),
        telemetry=_telemetry_ref(telemetry),
    )
    # Written once, before the worker exists. The worker writes every later
    # version, so this process never overwrites a state the worker has reached.
    scribe.jobs.write(job)
    argv = [sys.executable, "-m", "speak.transcribe.worker", str(settings.state_dir), job_id]
    try:
        await scribe.spawn(argv)
    except OSError as error:
        lock.release()
        scribe.jobs.write(
            replace(
                job, state=FAILED, finished_at=scribe.clock(), error=f"could not start: {error}"
            )
        )
        return f"could not start transcription: {error}"
    return (
        f"Transcribing {source.name} ({_duration(found.duration_s)} of audio"
        f"{', labelling speakers' if request.diarize else ''}) to {job.output}\n"
        f"Job {job_id}; about {_duration(job.eta_s)}. "
        "Ask transcription_status for progress."
    )


def status(scribe: Scribe, settings: Settings, job_id: str | None) -> str:
    if job_id is None:
        jobs = [_reconcile(scribe, job) for job in scribe.jobs.recent()]
        if not jobs:
            return "no transcriptions yet"
        return "\n".join(_summary(scribe, job) for job in jobs)
    job = scribe.jobs.read(job_id)
    if job is None:
        return f"no transcription job {job_id}"
    return _describe(scribe, settings, _reconcile(scribe, job))


def _reconcile(scribe: Scribe, job: Job) -> Job:
    """Turn a job whose worker is gone into the crash it is."""
    if job.state != RUNNING:
        return job
    if job.worker_pid is not None and scribe.is_alive(job.worker_pid):
        return job
    if job.worker_pid is None and scribe.clock() - job.created_at < WORKER_START_GRACE_S:
        return job
    if job.worker_pid is None and job.claimed_by == scribe.pid:
        # The worker never took the lock over, so this process still holds it.
        scribe.lock().release()
    crashed = replace(
        job,
        state=CRASHED,
        finished_at=scribe.clock(),
        error=job.error or "the worker stopped without recording an outcome",
    )
    scribe.jobs.write(crashed)
    return crashed


def _describe(scribe: Scribe, settings: Settings, job: Job) -> str:
    name = Path(job.source).name
    if job.state == DONE:
        text = transcript_text(Path(job.output), job.format)
        preview = text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
        return f"done: {name} -> {job.output} ({job.words} words)\n{preview}"
    if job.state == EMPTY:
        return f"finished, but no speech was detected in {name}; output: {job.output}"
    if job.state in (FAILED, CRASHED):
        tail = scribe.jobs.log_tail(job.job_id)
        return f"{job.state}: {name}: {job.error or ''}" + (f"\n{tail}" if tail else "")
    elapsed = scribe.clock() - job.created_at
    timeout = settings.transcribe_timeout if (settings.transcribe_timeout or 0) > 0 else None
    limit = timeout if timeout is not None else max(MIN_TIMEOUT_S, 4 * job.eta_s)
    if elapsed > limit:
        return (
            f"running: {name} for {_duration(elapsed)}, well past the "
            f"{_duration(job.eta_s)} estimate - it may be wedged"
        )
    remaining = max(0.0, job.eta_s - elapsed)
    return f"running: {name}, {_duration(elapsed)} elapsed, about {_duration(remaining)} left"


def _summary(scribe: Scribe, job: Job) -> str:
    return f"{job.job_id}  {job.state:8}  {Path(job.source).name}"


def _busy(scribe: Scribe) -> str:
    running = next((j for j in scribe.jobs.recent() if j.state == RUNNING), None)
    if running is None:
        return "a transcription is already running; only one runs at a time"
    left = max(0.0, running.eta_s - (scribe.clock() - running.created_at))
    return (
        f"a transcription is already running (job {running.job_id}, "
        f"about {_duration(left)} left); only one runs at a time"
    )


def _destination(argument: str | None, configured: Path | None, source: Path) -> Path:
    if argument:
        return Path(argument).expanduser()
    return configured if configured is not None else source.parent


def _telemetry_ref(telemetry: TelemetrySession | None) -> dict[str, str] | None:
    if telemetry is None or telemetry.db_path is None:
        return None
    return {
        "db_path": str(telemetry.db_path),
        "session_id": telemetry.session_id,
        "install_id": telemetry.install_id,
    }


def _refused(
    telemetry: TelemetrySession | None,
    settings: Settings,
    request: TranscribeRequest,
    found: Probe,
    *,
    audio: bool,
) -> None:
    if telemetry is None:
        return
    record_transcription(
        telemetry.conn,
        session_id=telemetry.session_id,
        install_id=telemetry.install_id,
        engine="nemo",
        model=settings.nemo_asr_model,
        outcome=TranscriptionOutcome(
            status="refused",
            source_bytes=found.size_bytes,
            audio_codec=found.codec,
            audio_ms=int(found.duration_s * 1000) if audio else None,
            asr_ms=None,
            converted=None,
            diarize=request.diarize,
            format=request.format if request.format in FORMATS else "text",
        ),
    )


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} s"
    return f"{round(seconds / 60)} min"
