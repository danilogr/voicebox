"""What the job tools share, whatever the kind: the store, the one-job lock, how a
worker is started, and how a worker that died is recognised."""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from speak_core.exclusive import ExclusiveLock
from speak_core.supervisor import process_is_alive

from voicebox.config import Settings
from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import CRASHED, FAILED, RUNNING, Job, JobStore
from voicebox.transcribe.probe import Probe, probe
from voicebox.transcribe.worker import Record, record_nothing

logger = logging.getLogger(__name__)

# How long a job may sit with no worker pid anywhere before it counts as never started.
WORKER_START_GRACE_S = 60.0

Spawn = Callable[[Sequence[str], Path], Awaitable[int]]
ProbeMedia = Callable[[Path], Awaitable[Probe]]


@dataclass(frozen=True)
class Desk:
    jobs: JobStore
    lock: Callable[[], ExclusiveLock]
    is_alive: Callable[[int], bool]
    spawn: Spawn
    # Transcription's ffprobe; here so a test can hand every job tool one fake.
    probe: ProbeMedia
    clock: Callable[[], float] = time.time
    pid: int = field(default_factory=os.getpid)
    # Called once with the final record of a job this desk could not start.
    record: Record = record_nothing


async def spawn_worker(argv: Sequence[str], log: Path) -> int:
    """Start a worker detached, with its stderr in the job's log.

    Detached, like the speech server: the job must outlive this session. Its stderr goes
    to the log rather than to `DEVNULL`, because a worker that fails before it can write
    a record would otherwise leave a job that merely stops, with nothing to say why.
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


def build_desk(settings: Settings) -> Desk:
    return Desk(
        jobs=JobStore(settings.state_dir),
        lock=lambda: job_lock(settings.state_dir, is_alive=process_is_alive),
        is_alive=process_is_alive,
        spawn=spawn_worker,
        probe=probe,
    )


def claim(desk: Desk) -> ExclusiveLock:
    """The one-job lock, claimed for a worker about to start. Raises one of
    `lock.BUSY` (`JobBusy`, or `ClaimInProgressError` while another session is mid-claim).

    The release first matters: a worker of ours that died before taking the lock over
    left it held by this session, and the lock is not re-entrant.
    """
    release_if_idle(desk)
    lock = desk.lock()
    lock.acquire()
    return lock


async def launch(desk: Desk, lock: ExclusiveLock, job: Job, argv: Sequence[str]) -> str | None:
    """Write a job's first record and start its worker. `None` once it is running;
    otherwise why it could not start, with the lock already given back.

    The record is written once, before the worker exists; the worker writes every later
    version, so this process never overwrites a state the worker has reached.
    """
    try:
        desk.jobs.write(job)
    except OSError as error:
        lock.release()
        return str(error)
    try:
        worker_pid = await desk.spawn(argv, desk.jobs.log_path(job.job_id))
    except OSError as error:
        lock.release()
        failed = replace(
            job, state=FAILED, finished_at=desk.clock(), error=f"could not start: {error}"
        )
        desk.jobs.write(failed)
        desk.record(failed)
        return str(error)
    # A sidecar the tool alone writes, never the record: it makes a worker that died
    # before writing its own pid detectable at once instead of after the grace window.
    try:
        desk.jobs.write_pid(job.job_id, worker_pid)
    except OSError as error:
        logger.debug("could not record the worker pid for job %s: %s", job.job_id, error)
    return None


def reconcile(desk: Desk, job: Job) -> Job:
    """Turn a job whose worker is gone into the crash it is."""
    if job.state != RUNNING:
        return job
    pid = job.worker_pid if job.worker_pid is not None else desk.jobs.read_pid(job.job_id)
    if pid is not None:
        if desk.is_alive(pid):
            return job
    elif desk.clock() - job.created_at < WORKER_START_GRACE_S:
        return job
    crashed = replace(
        job,
        state=CRASHED,
        finished_at=desk.clock(),
        error=job.error or "the worker stopped without recording an outcome",
    )
    desk.jobs.write(crashed)
    # The worker's own `finally` never ran, so nothing else will reclaim what it held.
    desk.jobs.discard_artifacts(job.job_id)
    return crashed


def release_if_idle(desk: Desk) -> None:
    """Give the lock back when it is ours and no job anywhere is still running.

    Checked install-wide and keyed on the lock rather than on who reconciles a job
    first: a worker that died before taking over leaves the lock with the session that
    claimed it, and another session's status call may be the one that marks that job
    crashed - after which the claiming session would never release it.
    """
    lock = desk.lock()
    if lock.owner() != desk.pid:
        return
    for job in desk.jobs.running():
        if reconcile(desk, job).state == RUNNING:
            return
    lock.release()


def sweep(desk: Desk) -> None:
    """Reconcile every running job once, at session start, so a worker that died while
    no session was asking is still found, and a lock it left is freed."""
    for job in desk.jobs.running():
        reconcile(desk, job)
    release_if_idle(desk)


def busy_message(desk: Desk) -> str:
    """The refusal for a live holder, used by every job tool and by the server's own
    in-process gate, so both ways of learning voicebox is busy say the same thing."""
    running = next((j for j in desk.jobs.recent() if j.state == RUNNING), None)
    if running is None:
        return "a voicebox job is already running; voicebox runs one job at a time"
    left = max(0.0, running.eta_s - (desk.clock() - running.created_at))
    return (
        f"a {running.kind} is already running (job {running.job_id}, "
        f"about {duration(left)} left); voicebox runs one job at a time"
    )


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} s"
    return f"{round(seconds / 60)} min"
