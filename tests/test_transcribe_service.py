import asyncio
import sys
from dataclasses import replace
from typing import Never

from fakes import installed
from voicebox.config import Settings
from voicebox.jobs.desk import Desk, spawn_worker
from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import DONE, FAILED, RUNNING, JobStore
from voicebox.transcribe.probe import Probe
from voicebox.transcribe.service import TranscribeRequest, start

ME, OTHER, WORKER = 100, 300, 4242
AUDIO = Probe(
    duration_s=690.0,
    size_bytes=49_283_072,
    container="matroska,webm",
    codec="flac",
    sample_rate=16000,
    channels=1,
)
VIDEO_ONLY = Probe(
    duration_s=14.4,
    size_bytes=408_576,
    container="mov,mp4,m4a,3gp,3g2,mj2",
    codec=None,
    sample_rate=None,
    channels=None,
)


def scribe(tmp_path, found=AUDIO, alive=(ME,), spawned=None, now=1000.0, pid=ME):
    spawned = spawned if spawned is not None else []

    async def probe(path) -> Probe:
        return found

    async def spawn(argv, log) -> int:
        spawned.append(list(argv))
        return WORKER

    return Desk(
        jobs=JobStore(tmp_path / "state", clock=lambda: now),
        lock=lambda: job_lock(tmp_path / "state", is_alive=lambda p: p in alive, pid=pid),
        is_alive=lambda p: p in alive,
        spawn=spawn,
        probe=probe,
        which=installed,
        clock=lambda: now,
        pid=pid,
    )


def request(path, **overrides: object):
    fields = dict(
        path=str(path),
        output_dir=None,
        format="text",
        diarize=True,
        language=None,
        translate_to=None,
    )
    return TranscribeRequest(**{**fields, **overrides})


def settings(tmp_path):
    return Settings(state_dir=tmp_path / "state")


def media(tmp_path, name="meeting.m4a"):
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


async def test_a_missing_file_is_named(tmp_path):
    reply = await start(scribe(tmp_path), settings(tmp_path), request(tmp_path / "gone.m4a"))
    assert "no such file" in reply


async def test_a_video_only_file_is_refused_before_anything_spawns(tmp_path):
    spawned = []
    s = scribe(tmp_path, found=VIDEO_ONLY, spawned=spawned)
    reply = await start(s, settings(tmp_path), request(media(tmp_path, "clip.mp4")))
    assert "no audio stream" in reply
    assert spawned == []


async def test_an_unknown_format_is_refused(tmp_path):
    reply = await start(
        scribe(tmp_path), settings(tmp_path), request(media(tmp_path), format="docx")
    )
    assert "unknown format" in reply


async def test_an_unwritable_destination_is_refused_before_the_job_starts(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        reply = await start(
            scribe(tmp_path),
            settings(tmp_path),
            request(media(tmp_path), output_dir=str(locked)),
        )
    finally:
        locked.chmod(0o700)
    assert "cannot write" in reply


async def test_a_missing_nemo_speech_is_refused_before_the_job_starts(tmp_path):
    spawned = []
    s = replace(scribe(tmp_path, spawned=spawned), which=lambda name: None)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)))
    assert "NeMo-Speech.cpp" in reply and "/voicebox:setup" in reply
    assert spawned == [] and s.jobs.recent() == [] and s.lock().owner() is None


async def test_a_relative_output_directory_is_refused(tmp_path):
    spawned = []
    s = scribe(tmp_path, spawned=spawned)
    reply = await start(s, settings(tmp_path), request(media(tmp_path), output_dir="out"))
    assert "must be an absolute path" in reply
    assert spawned == []


async def test_a_home_that_cannot_be_found_is_refused_not_raised(tmp_path):
    s = scribe(tmp_path)
    reply = await start(s, settings(tmp_path), request("~no-such-user-voicebox/a.m4a"))
    assert reply.startswith("cannot resolve the path")
    reply = await start(
        s, settings(tmp_path), request(media(tmp_path), output_dir="~no-such-user-voicebox/o")
    )
    assert reply.startswith("cannot resolve the output directory")
    assert s.jobs.recent() == []


async def test_a_started_job_spawns_the_worker_and_keeps_the_lock(tmp_path):
    spawned = []
    s = scribe(tmp_path, spawned=spawned)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)))
    (job,) = s.jobs.recent()
    assert job.state == RUNNING and job.claimed_by == ME and job.convert is True
    assert job.output == str(tmp_path / "meeting.txt")
    assert spawned[0][-3:] == ["voicebox.transcribe.worker", str(tmp_path / "state"), job.job_id]
    assert s.lock().owner() == ME
    assert job.job_id in reply and "meeting.txt" in reply


async def test_the_output_directory_argument_wins(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path), output_dir=str(out)))
    assert s.jobs.recent()[0].output == str(out / "meeting.txt")


async def test_a_job_sent_while_one_runs_waits_its_turn(tmp_path):
    s = scribe(tmp_path, alive=(ME, OTHER))
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    reply = await start(s, settings(tmp_path), request(media(tmp_path)))
    assert reply.startswith("Transcribing") and "queued, next in line" in reply
    assert [job.state for job in s.jobs.recent()] == ["queued"]


async def test_a_spawn_failure_releases_the_lock_and_fails_the_job(tmp_path):
    s = scribe(tmp_path)

    async def spawn(argv, log) -> Never:
        raise OSError("no python")

    recorded = []
    s = replace(s, spawn=spawn, record=recorded.append)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)))
    assert "could not start" in reply
    assert s.jobs.recent()[0].state == FAILED
    assert s.lock().owner() is None
    assert [job.state for job in recorded] == [FAILED]  # one telemetry row per job


async def test_a_record_that_cannot_be_written_frees_the_lock(tmp_path):
    s = scribe(tmp_path)

    class BrokenJobStore(JobStore):
        def write(self, job) -> None:
            raise OSError("disk full")

    s = replace(s, jobs=BrokenJobStore(tmp_path / "state"))
    reply = await start(s, settings(tmp_path), request(media(tmp_path)))
    assert "could not start" in reply
    assert s.lock().owner() is None


async def test_a_terminal_record_the_worker_wrote_is_never_reverted_by_the_tool(tmp_path):
    """The old code re-read the record after spawning and wrote the pid back onto
    whatever it saw; if the worker had already finished in that window, that write
    clobbered its outcome with a stale, running copy. The sidecar removes the write
    to the record entirely, so this can no longer happen."""
    jobs = JobStore(tmp_path / "state", clock=lambda: 1000.0)

    async def probe(path) -> Probe:
        return AUDIO

    async def spawn(argv, log) -> int:
        # The worker "wins the race": by the time `start()` gets back to recording
        # anything more about this job, the worker has already finished it.
        (running,) = jobs.recent()
        jobs.write(replace(running, state=DONE, worker_pid=WORKER, words=3, finished_at=1000.0))
        return WORKER

    s = Desk(
        jobs=jobs,
        lock=lambda: job_lock(tmp_path / "state", is_alive=lambda p: p in (ME, WORKER), pid=ME),
        is_alive=lambda p: p in (ME, WORKER),
        spawn=spawn,
        probe=probe,
        which=installed,
        clock=lambda: 1000.0,
        pid=ME,
    )

    await start(s, settings(tmp_path), request(media(tmp_path)))

    job = jobs.recent()[0]
    assert (job.state, job.words) == (DONE, 3)
    assert jobs.read_pid(job.job_id) == WORKER


async def test_three_calls_in_one_session_start_exactly_one_transcription(tmp_path):
    """A model told to transcribe three recordings emits three calls in one turn, and
    the lock is claimed for a worker that has not taken it over yet - so a lock that
    waved its own pid through would let all three spawn. All three are accepted, one
    runs, the others wait, and no two write the same transcript."""
    spawned = []
    s = scribe(tmp_path, alive=(ME, WORKER), spawned=spawned)
    source = media(tmp_path)
    replies = await asyncio.gather(
        *(start(s, settings(tmp_path), request(source)) for _ in range(3))
    )
    assert all(reply.startswith("Transcribing") for reply in replies)
    assert sum("queued, behind" in reply for reply in replies) == 2
    assert len(spawned) == 1
    jobs = s.jobs.recent()
    assert sorted(job.state for job in jobs) == ["queued", "queued", "running"]
    assert len({job.output for job in jobs}) == 3


async def test_the_spawned_worker_writes_its_stderr_to_the_job_log(tmp_path):
    """A worker that fails at the Python level - an import error, an unhandled
    exception - has nowhere else to say so: it is detached and nobody reads it."""
    log = tmp_path / "j1.log"
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('ModuleNotFoundError\\n')"]
    assert await spawn_worker(argv, log) > 0
    for _ in range(200):
        if log.exists() and "ModuleNotFoundError" in log.read_text():
            break
        await asyncio.sleep(0.02)
    assert "ModuleNotFoundError" in log.read_text()
