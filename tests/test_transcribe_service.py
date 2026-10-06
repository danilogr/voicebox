import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Never

from speak.settings import Settings
from speak.telemetry.session import MAX_AUDIO_MS, MAX_SOURCE_BYTES
from speak.transcribe.jobs import CRASHED, DONE, EMPTY, FAILED, RUNNING, JobStore
from speak.transcribe.lock import transcription_lock
from speak.transcribe.probe import Probe
from speak.transcribe.service import Scribe, TranscribeRequest, spawn_worker, start, status

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

    return Scribe(
        jobs=JobStore(tmp_path / "state", clock=lambda: now),
        lock=lambda: transcription_lock(tmp_path / "state", is_alive=lambda p: p in alive, pid=pid),
        is_alive=lambda p: p in alive,
        spawn=spawn,
        probe=probe,
        clock=lambda: now,
        pid=pid,
    )


class FakeTelemetry:
    """Only what a refusal reads off a session: a store handle and the two ids."""

    conn = None
    session_id = "s"
    install_id = "i"
    db_path = None


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
    reply = await start(scribe(tmp_path), settings(tmp_path), request(tmp_path / "gone.m4a"), None)
    assert "no such file" in reply


async def test_a_video_only_file_is_refused_before_anything_spawns(tmp_path):
    spawned = []
    s = scribe(tmp_path, found=VIDEO_ONLY, spawned=spawned)
    reply = await start(s, settings(tmp_path), request(media(tmp_path, "clip.mp4")), None)
    assert "no audio stream" in reply
    assert spawned == []


async def test_an_unknown_format_is_refused(tmp_path):
    reply = await start(
        scribe(tmp_path), settings(tmp_path), request(media(tmp_path), format="docx"), None
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
            None,
        )
    finally:
        locked.chmod(0o700)
    assert "cannot write" in reply


async def test_a_started_job_spawns_the_worker_and_keeps_the_lock(tmp_path):
    spawned = []
    s = scribe(tmp_path, spawned=spawned)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)), None)
    (job,) = s.jobs.recent()
    assert job.state == RUNNING and job.claimed_by == ME and job.convert is True
    assert job.output == str(tmp_path / "meeting.txt")
    assert spawned[0][-3:] == ["speak.transcribe.worker", str(tmp_path / "state"), job.job_id]
    assert s.lock().owner() == ME
    assert job.job_id in reply and "meeting.txt" in reply


async def test_the_output_directory_argument_wins(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path), output_dir=str(out)), None)
    assert s.jobs.recent()[0].output == str(out / "meeting.txt")


async def test_a_second_job_is_refused_while_one_runs(tmp_path):
    s = scribe(tmp_path, alive=(ME, OTHER))
    transcription_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    reply = await start(s, settings(tmp_path), request(media(tmp_path)), None)
    assert "already running" in reply


async def test_a_spawn_failure_releases_the_lock_and_fails_the_job(tmp_path):
    s = scribe(tmp_path)

    async def spawn(argv, log) -> Never:
        raise OSError("no python")

    s = replace(s, spawn=spawn)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)), None)
    assert "could not start" in reply
    assert s.jobs.recent()[0].state == FAILED
    assert s.lock().owner() is None


async def test_a_record_that_cannot_be_written_frees_the_lock(tmp_path):
    s = scribe(tmp_path)

    class BrokenJobStore(JobStore):
        def write(self, job) -> None:
            raise OSError("disk full")

    s = replace(s, jobs=BrokenJobStore(tmp_path / "state"))
    reply = await start(s, settings(tmp_path), request(media(tmp_path)), None)
    assert "could not start" in reply
    assert s.lock().owner() is None


async def test_status_reports_a_running_job_with_time_left(tmp_path):
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=ME))
    later = replace(s, clock=lambda: 1030.0)
    reply = status(later, settings(tmp_path), job.job_id)
    assert "running" in reply and "left" in reply


async def test_status_marks_a_dead_worker_as_crashed(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=9999))
    assert "crashed" in status(s, settings(tmp_path), job.job_id)
    assert s.jobs.read(job.job_id).state == CRASHED


async def test_a_crashed_jobs_wav_is_deleted(tmp_path):
    """The worker's own `finally` never ran, so nothing else reclaims the converted
    audio it left behind - a SIGKILL or an OOM kill is exactly this case."""
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=9999))
    wav = s.jobs.wav_path(job.job_id)
    wav.write_bytes(b"RIFF....WAVEfmt ")
    status(s, settings(tmp_path), job.job_id)
    assert not wav.exists()


async def test_a_worker_that_never_started_frees_the_lock(tmp_path):
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    # Neither the record nor the sidecar names a pid: only the grace window can tell
    # this job from one whose worker is still starting.
    assert job.worker_pid is None
    s.jobs.pid_path(job.job_id).unlink(missing_ok=True)
    much_later = replace(s, clock=lambda: 1000.0 + 120)
    assert "crashed" in status(much_later, settings(tmp_path), job.job_id)
    assert s.lock().owner() is None


async def test_a_dead_workers_sidecar_pid_is_crashed_without_waiting_out_the_grace_window(
    tmp_path,
):
    """The pid the spawn returned lives in a sidecar the tool alone writes - never on
    the job record, which stays `worker_pid=None` until the worker itself takes over -
    so a worker that died is a crash now rather than a job that looks healthy for
    another minute."""
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    assert job.worker_pid is None
    assert s.jobs.read_pid(job.job_id) == WORKER
    assert "crashed" in status(s, settings(tmp_path), job.job_id)


async def test_a_live_workers_sidecar_pid_is_not_crashed(tmp_path):
    """The same sidecar, but naming a pid that is still alive: the job is left
    running rather than judged by a grace window it does not need."""
    s = scribe(tmp_path, now=1000.0, alive=(ME, WORKER))
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    reply = status(s, settings(tmp_path), job.job_id)
    assert "running" in reply
    assert s.jobs.read(job.job_id).state == RUNNING


async def test_a_malformed_sidecar_does_not_raise(tmp_path):
    """Whatever corrupted it - a partial write, a hand edit - reading the sidecar
    must never raise out of a status call; it falls back to the grace window like a
    job with no pid recorded anywhere."""
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.pid_path(job.job_id).write_text("not-a-pid")
    reply = status(s, settings(tmp_path), job.job_id)  # must not raise
    assert "running" in reply


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

    s = Scribe(
        jobs=jobs,
        lock=lambda: transcription_lock(
            tmp_path / "state", is_alive=lambda p: p in (ME, WORKER), pid=ME
        ),
        is_alive=lambda p: p in (ME, WORKER),
        spawn=spawn,
        probe=probe,
        clock=lambda: 1000.0,
        pid=ME,
    )

    await start(s, settings(tmp_path), request(media(tmp_path)), None)

    job = jobs.recent()[0]
    assert (job.state, job.words) == (DONE, 3)
    assert jobs.read_pid(job.job_id) == WORKER


async def test_three_calls_in_one_session_start_exactly_one_transcription(tmp_path):
    """A model told to transcribe three recordings emits three calls in one turn, and
    the lock is claimed for a worker that has not taken it over yet - so a lock that
    waved its own pid through let all three spawn, onto the same output path."""
    spawned = []
    s = scribe(tmp_path, alive=(ME, WORKER), spawned=spawned)
    source = media(tmp_path)
    replies = await asyncio.gather(
        *(start(s, settings(tmp_path), request(source), None) for _ in range(3))
    )
    accepted = [reply for reply in replies if reply.startswith("Transcribing")]
    refused = [reply for reply in replies if "only one runs at a time" in reply]
    assert len(accepted) == 1
    assert len(refused) == 2
    assert len(spawned) == 1
    assert len(s.jobs.recent()) == 1


async def test_a_lock_wedged_by_a_dead_worker_is_freed_by_the_session_holding_it(tmp_path):
    """Session B reconciles the job first, so session A never sees it running again.
    The release has to key on the lock, or A wedges every other session for its life."""
    a = scribe(tmp_path, alive=(ME, OTHER))
    await start(a, settings(tmp_path), request(media(tmp_path)), None)
    b = scribe(tmp_path, alive=(ME, OTHER), pid=OTHER)

    assert "crashed" in status(b, settings(tmp_path), None)
    assert a.lock().owner() == ME, "the claiming session still holds it"
    assert "already running" in await start(b, settings(tmp_path), request(media(tmp_path)), None)

    status(a, settings(tmp_path), None)
    assert a.lock().owner() is None
    reply = await start(b, settings(tmp_path), request(media(tmp_path, "second.m4a")), None)
    assert reply.startswith("Transcribing")


async def test_status_gives_a_preview_of_a_finished_transcript(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    Path(job.output).write_text("word " * 400)
    s.jobs.write(replace(job, state=DONE, worker_pid=ME, words=400))
    reply = status(s, settings(tmp_path), job.job_id)
    assert job.output in reply
    assert len(reply) < 700


async def test_status_survives_a_transcript_that_has_been_moved(tmp_path):
    """The transcript is the user's file from the moment it lands, and asking after a
    job they have since filed away must not raise out of the tool."""
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, state=DONE, worker_pid=ME, words=400))
    reply = status(s, settings(tmp_path), job.job_id)
    assert reply.startswith("done:")
    assert "no longer at that path" in reply


async def test_status_says_when_no_speech_was_heard(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, state=EMPTY, worker_pid=ME))
    assert "no speech" in status(s, settings(tmp_path), job.job_id)


def test_status_without_jobs(tmp_path):
    assert status(scribe(tmp_path), settings(tmp_path), None) == "no transcriptions yet"


def test_status_of_an_unknown_job(tmp_path):
    assert "no transcription job" in status(scribe(tmp_path), settings(tmp_path), "zzz")


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


async def test_a_refused_file_past_the_ceilings_is_recorded_at_them(tmp_path, monkeypatch):
    """The refusal is where the largest files land - a file too big to ship a row
    about is exactly the one another session is most likely to be busy with."""
    seen = []
    monkeypatch.setattr(
        "speak.transcribe.service.record_transcription",
        lambda conn, **fields: seen.append(fields["outcome"]),
    )
    huge = replace(AUDIO, size_bytes=20 * 1024**3, duration_s=200_000.0)
    s = scribe(tmp_path, found=huge, alive=(ME, OTHER))
    transcription_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()

    reply = await start(s, settings(tmp_path), request(media(tmp_path)), FakeTelemetry())

    assert "already running" in reply
    (outcome,) = seen
    assert (outcome.source_bytes, outcome.audio_ms) == (MAX_SOURCE_BYTES, MAX_AUDIO_MS)
