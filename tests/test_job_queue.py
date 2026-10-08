"""The job queue: every accepted job waits as `queued`, and `start_next` - called by the
tools, by a finishing worker, at session start and by job_status - starts the oldest one
whenever no job runs."""

from dataclasses import replace
from pathlib import Path

import pytest

from fakes import installed
from voicebox.config import Settings
from voicebox.jobs import desk as desk_module
from voicebox.jobs.desk import Desk, queue, start_next
from voicebox.jobs.lock import job_lock
from voicebox.jobs.status import job_status as status
from voicebox.jobs.store import FAILED, QUEUED, RUNNING, JobStore, TranscriptionJob

ME, OTHER, WORKER = 100, 300, 4242


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def desk_at(tmp_path: Path, *, alive=(ME,), clock=None, spawn=None, pid=ME, spawned=None) -> Desk:
    clock = clock or Clock()
    spawned = spawned if spawned is not None else []

    async def spawn_ok(argv, log) -> int:
        spawned.append(list(argv))
        return WORKER

    return Desk(
        jobs=JobStore(tmp_path / "state", clock=clock),
        lock=lambda: job_lock(tmp_path / "state", is_alive=lambda p: p in alive, pid=pid),
        is_alive=lambda p: p in alive,
        spawn=spawn or spawn_ok,
        probe=None,  # type: ignore[arg-type]
        which=installed,
        clock=clock,
        monotonic=clock,
        pid=pid,
    )


def job(name: str, created_at: float, **overrides: object) -> TranscriptionJob:
    fields = dict(
        job_id=f"00000000-0000-4000-8000-{name:0>12}",
        state=QUEUED,
        source=f"/audio/{name}.m4a",
        output=f"/audio/{name}.txt",
        format="text",
        nemo_binary="nemo-speech",
        claimed_by=ME,
        created_at=created_at,
        diarize=False,
        language=None,
        translate_to=None,
        duration_s=95.0,  # 10 s of work at 9.5x
        size_bytes=1,
        codec="flac",
        convert=False,
        asr_model="nemotron-3.5",
    )
    return TranscriptionJob(**{**fields, **overrides})  # type: ignore[arg-type]


async def test_the_oldest_queued_job_starts_first(tmp_path):
    spawned: list[list[str]] = []
    desk = desk_at(tmp_path, spawned=spawned)
    for name, at in (("b", 20.0), ("a", 10.0), ("c", 30.0)):
        queue(desk, job(name, at))

    started = await start_next(desk)

    assert started is not None and started.source == "/audio/a.m4a"
    assert [j.source for j in desk.jobs.queued()] == ["/audio/b.m4a", "/audio/c.m4a"]
    assert spawned[0][-1] == started.job_id
    assert spawned[0][2] == "voicebox.transcribe.worker"


async def test_nothing_starts_while_a_live_job_holds_the_lock(tmp_path):
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    desk = desk_at(tmp_path, alive=(ME, OTHER))
    queue(desk, job("a", 10.0))

    assert await start_next(desk) is None
    assert [j.state for j in desk.jobs.recent()] == [QUEUED]


async def test_the_queue_moves_on_after_a_worker_that_died(tmp_path):
    """A worker killed mid-job leaves the lock naming a dead pid: whoever calls next
    takes it and starts the next job."""
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    desk = desk_at(tmp_path, alive=(ME,))
    queue(desk, job("a", 10.0))

    started = await start_next(desk)

    assert started is not None
    assert desk.jobs.read(started.job_id).state == RUNNING


async def test_a_started_job_measures_its_time_from_when_it_started(tmp_path):
    clock = Clock(1000.0)
    desk = desk_at(tmp_path, clock=clock)
    queue(desk, job("a", 10.0))
    clock.now = 2000.0

    started = await start_next(desk)

    record = desk.jobs.read(started.job_id)
    assert (record.created_at, record.created_monotonic, record.queued_at) == (
        2000.0,
        2000.0,
        10.0,
    )
    assert record.claimed_by == ME


async def test_a_job_whose_worker_cannot_start_fails_and_the_next_one_starts(tmp_path):
    calls = []

    async def spawn(argv, log) -> int:
        calls.append(argv[-1])
        if len(calls) == 1:
            raise OSError("no python")
        return WORKER

    desk = desk_at(tmp_path, spawn=spawn)
    queue(desk, job("a", 10.0))
    queue(desk, job("b", 20.0))

    started = await start_next(desk)

    assert started is not None and started.source == "/audio/b.m4a"
    first = desk.jobs.read(job("a", 10.0).job_id)
    assert (first.state, first.reason) == (FAILED, "other")


async def test_a_job_whose_failure_cannot_be_written_is_dropped_not_retried(tmp_path):
    """A record that cannot be rewritten would otherwise stay queued and be tried again
    on every call, failing each time."""

    class ReadOnlyAfterQueue(JobStore):
        writes = 0

        def write(self, job) -> None:
            type(self).writes += 1
            if job.state != QUEUED:
                raise OSError("disk full")
            super().write(job)

    async def spawn(argv, log) -> int:
        raise AssertionError("never reached: the running record cannot be written")

    desk = replace(desk_at(tmp_path, spawn=spawn), jobs=ReadOnlyAfterQueue(tmp_path / "state"))
    queue(desk, job("a", 10.0))

    assert await start_next(desk) is None
    assert desk.jobs.queued() == []
    assert desk.lock().owner() is None


async def test_with_no_queued_job_the_lock_is_given_back(tmp_path):
    desk = desk_at(tmp_path)
    assert await start_next(desk) is None
    assert desk.lock().owner() is None


async def test_a_finishing_worker_waits_for_the_next_worker_to_take_the_lock(tmp_path, monkeypatch):
    """Until the new worker takes over, the lock names the old one; once that exits, a
    lock naming a dead pid could be reclaimed and a second job started."""
    looks = []

    async def sleep(seconds: float) -> None:
        looks.append(seconds)
        if len(looks) == 3:
            job_lock(tmp_path / "state", is_alive=lambda p: True, pid=WORKER).take_over(ME)

    monkeypatch.setattr(desk_module.asyncio, "sleep", sleep)
    desk = desk_at(tmp_path, alive=(ME, WORKER))
    queue(desk, job("a", 10.0))

    await start_next(desk, hand_off=True)

    assert len(looks) == 3
    assert desk.lock().owner() == WORKER


async def test_the_hand_off_gives_up_on_a_worker_that_died(tmp_path, monkeypatch):
    async def sleep(seconds: float) -> None:
        raise AssertionError("a dead worker is not waited for")

    monkeypatch.setattr(desk_module.asyncio, "sleep", sleep)
    desk = desk_at(tmp_path, alive=(ME,))  # the spawned WORKER is not alive
    queue(desk, job("a", 10.0))

    assert await start_next(desk, hand_off=True) is not None


def test_pruning_never_touches_a_queued_job_or_its_staged_text(tmp_path):
    jobs = JobStore(tmp_path / "state")
    waiting = job("a", 10.0)
    jobs.write(waiting)
    jobs.text_path(waiting.job_id).write_text("spoken later")

    jobs.recent()

    assert jobs.text_path(waiting.job_id).read_text() == "spoken later"


async def test_status_gives_a_queued_jobs_place_and_start(tmp_path):
    clock = Clock(1000.0)
    desk = desk_at(tmp_path, alive=(ME, WORKER), clock=clock)
    queue(desk, job("a", 10.0))
    queue(desk, job("b", 20.0))
    queue(desk, job("c", 30.0))
    await start_next(desk)  # a runs, 10 s of work
    clock.now = 1004.0

    report = status(desk, Settings(state_dir=tmp_path / "state"), job("c", 30.0).job_id)

    # 6 s left of a, then 10 s of b.
    assert report.startswith("queued: c.m4a, behind 2 jobs; it starts on its own in about 16 s")
    assert "done about 26 s from now" in report


@pytest.mark.parametrize(
    ("reason", "advice"),
    [
        ("timeout", "Sending the same request again may work."),
        ("model_failed", "Sending the same request again may work."),
        ("nemo_missing", "Run /voicebox:setup"),
        ("input_missing", "check the path"),
        ("output_unwritable", "choose another output path"),
    ],
)
def test_a_failure_says_whether_retrying_can_help(tmp_path, reason, advice):
    desk = desk_at(tmp_path)
    failed = job("a", 10.0, state=FAILED, reason=reason, error="it broke", finished_at=20.0)
    desk.jobs.write(failed)

    report = status(desk, Settings(state_dir=tmp_path / "state"), failed.job_id)

    assert report.startswith("failed: a.m4a: it broke\n")
    assert advice in report


def test_a_crash_says_retrying_may_work(tmp_path):
    desk = desk_at(tmp_path)
    crashed = job("a", 10.0, state="crashed", finished_at=20.0, error="the worker stopped")
    desk.jobs.write(crashed)

    report = status(desk, Settings(state_dir=tmp_path / "state"), crashed.job_id)

    assert "Sending the same request again may work." in report


@pytest.mark.parametrize("module", ["voicebox.transcribe.worker", "voicebox.synthesize.worker"])
@pytest.mark.parametrize("outcome", ["finished", "raised"])
def test_a_worker_starts_the_next_job_however_its_own_ended(monkeypatch, module, outcome):
    import importlib

    worker = importlib.import_module(module)
    started: list[Path] = []

    async def run_job(state_dir: Path, job_id: str, **kwargs: object) -> None:
        if outcome == "raised":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(worker, "run_job", run_job)
    monkeypatch.setattr(worker, "start_next_job", started.append)

    worker.main(["/state", "00000000-0000-4000-8000-000000000001"])

    assert started == [Path("/state")]


async def test_session_start_starts_a_job_left_waiting(tmp_path):
    from voicebox.__main__ import _start_waiting_job

    desk = desk_at(tmp_path)
    queue(desk, job("a", 10.0))

    await _start_waiting_job(desk)

    assert desk.jobs.read(job("a", 10.0).job_id).state == RUNNING


async def test_session_start_survives_a_state_dir_it_cannot_use(tmp_path):
    from voicebox.__main__ import _start_waiting_job

    (tmp_path / "state").write_text("a file where the directory should be")
    await _start_waiting_job(desk_at(tmp_path))  # logs, never raises


async def test_a_job_with_nothing_ahead_is_next_in_line(tmp_path):
    """The lock can be held for a moment with no job running yet: nothing is ahead."""
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    desk = desk_at(tmp_path, alive=(ME, OTHER))
    queue(desk, job("a", 10.0))

    report = status(desk, Settings(state_dir=tmp_path / "state"), job("a", 10.0).job_id)

    assert report.startswith("queued: a.m4a, next in line; it starts on its own in about 0 s")


def test_a_job_no_longer_queued_has_nothing_ahead(tmp_path):
    desk = desk_at(tmp_path)
    queue(desk, job("b", 20.0))
    assert desk_module.place(desk, job("a", 10.0)) == (0, 0.0)
