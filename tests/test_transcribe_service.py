from dataclasses import replace
from pathlib import Path
from typing import Never

from speak.config import Settings
from speak.transcribe.jobs import CRASHED, DONE, EMPTY, FAILED, RUNNING, JobStore
from speak.transcribe.lock import transcription_lock
from speak.transcribe.probe import Probe
from speak.transcribe.service import Scribe, TranscribeRequest, start, status

ME, OTHER = 100, 300
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


def scribe(tmp_path, found=AUDIO, alive=(ME,), spawned=None, now=1000.0):
    spawned = spawned if spawned is not None else []

    async def probe(path) -> Probe:
        return found

    async def spawn(argv) -> int:
        spawned.append(list(argv))
        return 4242

    return Scribe(
        jobs=JobStore(tmp_path / "state"),
        lock=lambda: transcription_lock(tmp_path / "state", is_alive=lambda p: p in alive, pid=ME),
        is_alive=lambda p: p in alive,
        spawn=spawn,
        probe=probe,
        clock=lambda: now,
        pid=ME,
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

    async def spawn(argv) -> Never:
        raise OSError("no python")

    s = replace(s, spawn=spawn)
    reply = await start(s, settings(tmp_path), request(media(tmp_path)), None)
    assert "could not start" in reply
    assert s.jobs.recent()[0].state == FAILED
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


async def test_a_worker_that_never_started_frees_the_lock(tmp_path):
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    much_later = replace(s, clock=lambda: 1000.0 + 120)
    assert "crashed" in status(much_later, settings(tmp_path), job.job_id)
    assert s.lock().owner() is None


async def test_status_gives_a_preview_of_a_finished_transcript(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)), None)
    job = s.jobs.recent()[0]
    Path(job.output).write_text("word " * 400)
    s.jobs.write(replace(job, state=DONE, worker_pid=ME, words=400))
    reply = status(s, settings(tmp_path), job.job_id)
    assert job.output in reply
    assert len(reply) < 700


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
