"""The transcribe and transcription_status tools, minus the MCP plumbing.

`server.py` defines the tools and calls these; nothing here knows about MCP,
which keeps the transport layer free of transcription logic.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from speak_core.exclusive import ExclusiveLock
from speak_core.supervisor import process_is_alive

from speak.settings import Settings
from speak.telemetry.session import (
    TelemetrySession,
    TranscriptionOutcome,
    clamped,
    record_transcription,
)
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

logger = logging.getLogger(__name__)

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
    spawn: Callable[[Sequence[str], Path], Awaitable[int]]
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


async def spawn_worker(argv: Sequence[str], log: Path) -> int:
    """Start the worker detached, with its stderr in the job's log.

    Detached, like the speech server: the job must outlive this session. Its stderr
    goes to the log rather than to `DEVNULL`, because a worker that fails before it
    can write a record - an import error, an unhandled exception, a takeover it was
    refused - would otherwise leave a job that merely stops, with nothing anywhere
    to say why. `transcription_status` already shows the tail of that log.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as sink:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=sink,
            start_new_session=True,
        )
    return process.pid


def build_scribe(settings: Settings) -> Scribe:
    return Scribe(
        jobs=JobStore(settings.state_dir),
        lock=lambda: transcription_lock(settings.state_dir, is_alive=process_is_alive),
        is_alive=process_is_alive,
        spawn=spawn_worker,
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

    # Before claiming: a worker of ours that died before taking the lock over left
    # it held by this session, and the lock is not re-entrant, so without this a
    # session would refuse itself for as long as it lived.
    _release_if_idle(scribe)
    lock = scribe.lock()
    try:
        lock.acquire()
    except TranscriptionBusy:
        _refused(telemetry, settings, request, found, audio=True)
        return busy_message(scribe)

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
        asr_model=settings.server.asr_model,
        nemo_binary=settings.server.binary,
        claimed_by=scribe.pid,
        created_at=scribe.clock(),
        telemetry=_telemetry_ref(telemetry),
    )
    # Written once, before the worker exists. The worker writes every later
    # version, so this process never overwrites a state the worker has reached.
    try:
        scribe.jobs.write(job)
    except OSError as error:
        lock.release()
        return f"could not start transcription: {error}"
    argv = [sys.executable, "-m", "speak.transcribe.worker", str(settings.state_dir), job_id]
    try:
        worker_pid = await scribe.spawn(argv, scribe.jobs.log_path(job_id))
    except OSError as error:
        lock.release()
        scribe.jobs.write(
            replace(
                job, state=FAILED, finished_at=scribe.clock(), error=f"could not start: {error}"
            )
        )
        return f"could not start transcription: {error}"
    # A sidecar the tool alone writes, never the job record: `create_subprocess_exec`
    # starts the worker itself, so this is the same pid the worker writes onto the
    # record a moment later, and having it early is what makes a worker that died
    # before that write detectable at once instead of only after
    # `WORKER_START_GRACE_S`. A record write here instead would be a read-modify-write
    # racing the worker's own writes to that record - which is exactly how a finished
    # job's terminal state used to get reverted back to running.
    try:
        scribe.jobs.write_pid(job_id, worker_pid)
    except OSError as error:
        # The job runs either way; without the sidecar it is reconciled by the grace
        # window instead of at once.
        logger.debug("could not record the worker pid for job %s: %s", job_id, error)
    return (
        f"Transcribing {source.name} ({_duration(found.duration_s)} of audio"
        f"{', labelling speakers' if request.diarize else ''}) to {job.output}\n"
        f"Job {job_id}; about {_duration(job.eta_s)}. "
        "Ask transcription_status for progress."
    )


def status(scribe: Scribe, settings: Settings, job_id: str | None) -> str:
    if job_id is None:
        jobs = [_reconcile(scribe, job) for job in scribe.jobs.recent()]
        _release_if_idle(scribe)
        if not jobs:
            return "no transcriptions yet"
        return "\n".join(_summary(job) for job in jobs)
    job = scribe.jobs.read(job_id)
    if job is None:
        return f"no transcription job {job_id}"
    described = _describe(scribe, settings, _reconcile(scribe, job))
    _release_if_idle(scribe)
    return described


def _reconcile(scribe: Scribe, job: Job) -> Job:
    """Turn a job whose worker is gone into the crash it is."""
    if job.state != RUNNING:
        return job
    # The job record's own `worker_pid` - written by the worker once it takes the
    # lock over - is the pid of record once it exists; until then the sidecar the
    # tool wrote right after spawning stands in for it.
    pid = job.worker_pid if job.worker_pid is not None else scribe.jobs.read_pid(job.job_id)
    if pid is not None:
        # A pid is on hand from one source or the other, so a dead one is a crash
        # now: the grace window below belongs only to a job with no pid anywhere.
        if scribe.is_alive(pid):
            return job
    elif scribe.clock() - job.created_at < WORKER_START_GRACE_S:
        return job
    crashed = replace(
        job,
        state=CRASHED,
        finished_at=scribe.clock(),
        error=job.error or "the worker stopped without recording an outcome",
    )
    scribe.jobs.write(crashed)
    # The worker is gone, so its own `finally` never ran to delete this. Nothing
    # else will: an hours-long recording left behind is ~115 MB per hour, unclaimed.
    scribe.jobs.wav_path(job.job_id).unlink(missing_ok=True)
    return crashed


def _release_if_idle(scribe: Scribe) -> None:
    """Give the lock back when it is ours and no job anywhere is still running.

    Checked install-wide - every running job, not only ones this session started -
    and keyed on the lock rather than on who reconciles a job first. A worker that
    died before taking over leaves the lock with the session that claimed it, and
    any session's `transcription_status` may be the one that marks that job
    crashed - after which the claiming session, reconciling a job that is no
    longer running, would never reach a release and would wedge the lock for every
    other session until it quit. Called from both `start()` and `status()`, so a
    session that never happens to ask after the dead job still lets the lock go
    the next time it starts one of its own.
    """
    lock = scribe.lock()
    if lock.owner() != scribe.pid:
        return
    for job in scribe.jobs.running():
        if _reconcile(scribe, job).state == RUNNING:
            return
    lock.release()


def _describe(scribe: Scribe, settings: Settings, job: Job) -> str:
    name = Path(job.source).name
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


def _preview(job: Job) -> str | None:
    """The first of a finished transcript, or None when it is not there to read.

    The file belongs to the user from the moment it is written: moving it, renaming
    it or deleting it is ordinary, and none of that should turn asking after a
    finished job into a traceback. `JobStore.log_tail` reads its file the same way.
    """
    try:
        text = transcript_text(Path(job.output), job.format)
    except OSError:
        return None
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


def _summary(job: Job) -> str:
    return f"{job.job_id}  {job.state:8}  {Path(job.source).name}"


def busy_message(scribe: Scribe) -> str:
    """The refusal `start()` gives for a live holder, exposed for `server.py`'s own
    fast-path gate so both ways of learning a transcription is busy say the same
    thing."""
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
        model=settings.server.asr_model,
        # Clamped for the same reason the worker's outcome is: the file that was
        # refused may be the very one whose size is over the contract's ceiling.
        outcome=clamped(
            TranscriptionOutcome(
                status="refused",
                source_bytes=found.size_bytes,
                audio_codec=found.codec,
                audio_ms=int(found.duration_s * 1000) if audio else None,
                asr_ms=None,
                converted=None,
                diarize=request.diarize,
                format=request.format,
            )
        ),
    )


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} s"
    return f"{round(seconds / 60)} min"
