"""What the job tools share, whatever the kind: the store, the queue and the one-job
lock, how a worker is started, and how a worker that died is recognised.

Every accepted job is written `queued`; `start_next` is the one way a job starts. It
claims the one-job lock and starts the oldest queued job, and is called by the tool
after queuing, by a worker that has just finished, at session start and by
`job_status`, so the queue moves on after a worker that died too.
"""

import asyncio
import logging
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from speak_core.exclusive import ExclusiveLock
from speak_core.supervisor import process_is_alive

from voicebox.config import Settings
from voicebox.jobs.lock import BUSY, job_lock
from voicebox.jobs.store import (
    CRASHED,
    FAILED,
    QUEUED,
    RUNNING,
    SYNTHESIS,
    TRANSCRIPTION,
    Job,
    JobStore,
)
from voicebox.telemetry.jobs import record_job
from voicebox.transcribe.probe import Probe, probe

logger = logging.getLogger(__name__)

# How long a job may sit with no worker pid anywhere before it counts as never started.
WORKER_START_GRACE_S = 60.0
# How long a worker that started the next job waits for that job's worker to take the
# lock over before exiting: until then the lock names this worker, and once it exits a
# lock naming a dead pid may be reclaimed by anyone, starting a second job at once.
HANDOFF_WAIT_S = 10.0
HANDOFF_POLL_S = 0.05

# The module each kind of job's worker runs as.
WORKERS = {TRANSCRIPTION: "voicebox.transcribe.worker", SYNTHESIS: "voicebox.synthesize.worker"}

Spawn = Callable[[Sequence[str], Path], Awaitable[int]]
ProbeMedia = Callable[[Path], Awaitable[Probe]]
# Called once with a job's final record, whoever ended it.
Record = Callable[[Job], None]


def record_nothing(job: Job) -> None:
    """The default when nobody records finished jobs (a test, or telemetry off)."""


@dataclass(frozen=True)
class Desk:
    jobs: JobStore
    lock: Callable[[], ExclusiveLock]
    is_alive: Callable[[int], bool]
    spawn: Spawn
    # Transcription's ffprobe; here so a test can hand every job tool one fake.
    probe: ProbeMedia
    # Finds a program on `PATH`, as a worker would: here so a test can say what is installed.
    which: Callable[[str], str | None] = shutil.which
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    pid: int = field(default_factory=os.getpid)
    # Called with the final record of a job this desk ends: one it could not start, or
    # one whose worker died.
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
    return desk_for(settings.state_dir)


def desk_for(state_dir: Path) -> Desk:
    """The desk for a state directory: what a worker that starts the next job uses."""
    return Desk(
        jobs=JobStore(state_dir),
        lock=lambda: job_lock(state_dir, is_alive=process_is_alive),
        is_alive=process_is_alive,
        spawn=spawn_worker,
        probe=probe,
        record=record_job,
    )


def queue(desk: Desk, job: Job) -> str | None:
    """Write an accepted job's first record, `queued`. `None` once written; otherwise
    why it could not be, with the job recorded as failed."""
    try:
        desk.jobs.write(replace(job, state=QUEUED, queued_at=job.created_at))
    except OSError as error:
        record_unstarted(desk, job, error)
        return str(error)
    return None


async def start_next(desk: Desk, *, hand_off: bool = False) -> Job | None:
    """Start the oldest queued job when no job runs: the job started, or `None`.

    Only the holder of the one-job lock starts a job, so two callers never start two.
    A job whose worker cannot be started is failed and the next one tried. `hand_off`
    is for a worker about to exit: it waits for the new worker to take the lock over.
    """
    tried: set[str] = set()  # a job whose failure could not be written stays queued
    while True:
        try:
            lock = claim(desk)
        except BUSY:
            return None
        waiting = [job for job in desk.jobs.queued() if job.job_id not in tried]
        if not waiting:
            lock.release()
            return None
        tried.add(waiting[0].job_id)
        started = replace(
            waiting[0],
            state=RUNNING,
            claimed_by=desk.pid,
            created_at=desk.clock(),
            created_monotonic=desk.monotonic(),
        )
        argv = [
            sys.executable,
            "-m",
            WORKERS[started.kind],
            str(desk.jobs.state_dir),
            started.job_id,
        ]
        if await launch(desk, lock, started, argv) is None:
            if hand_off:
                await _await_takeover(desk, desk.jobs.read_pid(started.job_id))
            return started
        desk.jobs.discard_artifacts(started.job_id)
        left = desk.jobs.read(started.job_id)
        if left is not None and left.state in (QUEUED, RUNNING):
            # Its failure could not be written: drop it, rather than retry it forever or
            # leave a running job no worker will ever finish.
            try:
                desk.jobs.record_path(started.job_id).unlink(missing_ok=True)
            except OSError as error:
                logger.warning("could not drop job %s: %s", started.job_id, error)


def start_next_job(state_dir: Path) -> None:
    """What a worker does as it exits, whatever its job's outcome: start the next queued
    job and see it take the lock over. Never raises - the worker's own job is over."""
    try:
        asyncio.run(start_next(desk_for(state_dir), hand_off=True))
    except Exception:
        logger.exception("could not start the next queued job")


async def _await_takeover(desk: Desk, worker_pid: int | None) -> None:
    deadline = desk.monotonic() + HANDOFF_WAIT_S
    lock = desk.lock()
    while worker_pid is not None and desk.monotonic() < deadline:
        if lock.owner() == worker_pid or not desk.is_alive(worker_pid):
            return
        await asyncio.sleep(HANDOFF_POLL_S)
    if worker_pid is not None:
        logger.warning("worker %s did not take the job lock over in time", worker_pid)


def place(desk: Desk, job: Job) -> tuple[int, float]:
    """How many jobs are ahead of a queued one - running or queued before it - and the
    seconds until it should start: what the running job has left, and the estimate of
    every queued job ahead of it."""
    queued = desk.jobs.queued()
    ids = [j.job_id for j in queued]
    ahead = queued[: ids.index(job.job_id)] if job.job_id in ids else []
    running = desk.jobs.running()
    wait = sum(max(0.0, j.eta_s - (desk.clock() - j.created_at)) for j in running) + sum(
        j.eta_s for j in ahead
    )
    return len(running) + len(ahead), wait


def behind(count: int) -> str:
    """Where a queued job stands, for the tools' answers."""
    if count == 0:
        return "next in line"
    return "behind 1 job" if count == 1 else f"behind {count} jobs"


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


def record_unstarted(desk: Desk, job: Job, error: OSError) -> None:
    """A job that could not start still ends in exactly one row: its record is kept when
    it can be written, and counted either way."""
    failed = replace(
        job,
        state=FAILED,
        finished_at=desk.clock(),
        error=f"could not start: {error}",
        reason="other",
    )
    try:
        desk.jobs.write(failed)
    except OSError as write_error:
        logger.debug("could not record job %s as failed: %s", job.job_id, write_error)
    desk.record(failed)


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
        record_unstarted(desk, job, error)
        return str(error)
    try:
        worker_pid = await desk.spawn(argv, desk.jobs.log_path(job.job_id))
    except OSError as error:
        lock.release()
        record_unstarted(desk, job, error)
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
    desk.record(crashed)
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


def failed_to_start(desk: Desk, job: Job) -> str | None:
    """Why `job` could not start when `start_next` just tried it, or `None`."""
    current = desk.jobs.read(job.job_id)
    if current is None:
        return "its worker could not be started, and neither could that be recorded"
    if current.state == FAILED:
        return current.error or "its worker could not be started"
    return None


def accepted(desk: Desk, job: Job) -> str:
    """What a tool answers once a job is accepted: its id, whether it is running or
    waiting its turn, and when it should finish."""
    current = desk.jobs.read(job.job_id) or job
    if current.state != QUEUED:
        return f"Job {job.job_id}; about {duration(job.eta_s)}. Ask job_status for progress."
    count, wait = place(desk, current)
    return (
        f"Job {job.job_id}; queued, {behind(count)}: voicebox runs one job at a time, and "
        f"this one starts on its own in about {duration(wait)} and should be done about "
        f"{duration(wait + job.eta_s)} from now. Ask job_status for progress."
    )


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} s"
    return f"{round(seconds / 60)} min"
