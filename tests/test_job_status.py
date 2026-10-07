from dataclasses import replace
from pathlib import Path

from voicebox.config import Settings
from voicebox.jobs.desk import Desk
from voicebox.jobs.lock import job_lock
from voicebox.jobs.status import job_status as status
from voicebox.jobs.store import CRASHED, DONE, EMPTY, RUNNING, JobStore
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


async def test_status_reports_a_running_job_with_time_left(tmp_path):
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=ME))
    later = replace(s, clock=lambda: 1030.0)
    reply = status(later, settings(tmp_path), job.job_id)
    assert "running" in reply and "left" in reply


async def test_status_marks_a_dead_worker_as_crashed(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=9999))
    assert "crashed" in status(s, settings(tmp_path), job.job_id)
    assert s.jobs.read(job.job_id).state == CRASHED


async def test_a_crashed_jobs_wav_is_deleted(tmp_path):
    """The worker's own `finally` never ran, so nothing else reclaims the converted
    audio it left behind - a SIGKILL or an OOM kill is exactly this case."""
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=9999))
    wav = s.jobs.wav_path(job.job_id)
    wav.write_bytes(b"RIFF....WAVEfmt ")
    status(s, settings(tmp_path), job.job_id)
    assert not wav.exists()


async def test_a_worker_that_never_started_frees_the_lock(tmp_path):
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)))
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
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    assert job.worker_pid is None
    assert s.jobs.read_pid(job.job_id) == WORKER
    assert "crashed" in status(s, settings(tmp_path), job.job_id)


async def test_a_live_workers_sidecar_pid_is_not_crashed(tmp_path):
    """The same sidecar, but naming a pid that is still alive: the job is left
    running rather than judged by a grace window it does not need."""
    s = scribe(tmp_path, now=1000.0, alive=(ME, WORKER))
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    reply = status(s, settings(tmp_path), job.job_id)
    assert "running" in reply
    assert s.jobs.read(job.job_id).state == RUNNING


async def test_a_malformed_sidecar_does_not_raise(tmp_path):
    """Whatever corrupted it - a partial write, a hand edit - reading the sidecar
    must never raise out of a status call; it falls back to the grace window like a
    job with no pid recorded anywhere."""
    s = scribe(tmp_path, now=1000.0)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.pid_path(job.job_id).write_text("not-a-pid")
    reply = status(s, settings(tmp_path), job.job_id)  # must not raise
    assert "running" in reply


async def test_a_lock_wedged_by_a_dead_worker_is_freed_by_the_session_holding_it(tmp_path):
    """Session B reconciles the job first, so session A never sees it running again.
    The release has to key on the lock, or A wedges every other session for its life."""
    a = scribe(tmp_path, alive=(ME, OTHER))
    await start(a, settings(tmp_path), request(media(tmp_path)))
    b = scribe(tmp_path, alive=(ME, OTHER), pid=OTHER)

    assert "crashed" in status(b, settings(tmp_path), None)
    assert a.lock().owner() == ME, "the claiming session still holds it"
    assert "already running" in await start(b, settings(tmp_path), request(media(tmp_path)))

    status(a, settings(tmp_path), None)
    assert a.lock().owner() is None
    reply = await start(b, settings(tmp_path), request(media(tmp_path, "second.m4a")))
    assert reply.startswith("Transcribing")


async def test_status_gives_a_preview_of_a_finished_transcript(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
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
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, state=DONE, worker_pid=ME, words=400))
    reply = status(s, settings(tmp_path), job.job_id)
    assert reply.startswith("done:")
    assert "no longer at that path" in reply


async def test_status_says_when_no_speech_was_heard(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, state=EMPTY, worker_pid=ME))
    assert "no speech" in status(s, settings(tmp_path), job.job_id)


def test_status_without_jobs(tmp_path):
    assert status(scribe(tmp_path), settings(tmp_path), None) == "no jobs yet"


def test_status_of_an_unknown_job(tmp_path):
    assert "no job zzz" in status(scribe(tmp_path), settings(tmp_path), "zzz")


async def test_the_list_names_each_jobs_kind(tmp_path):
    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    (line,) = status(s, settings(tmp_path), None).splitlines()
    assert "transcription" in line and "meeting.m4a" in line


async def test_a_busy_reply_names_the_running_jobs_kind(tmp_path):
    from voicebox.jobs.desk import busy_message

    s = scribe(tmp_path, alive=(ME, WORKER))
    await start(s, settings(tmp_path), request(media(tmp_path)))
    assert busy_message(s).startswith("a transcription is already running (job ")


async def test_the_session_start_sweep_crashes_a_dead_worker_and_frees_the_lock(tmp_path):
    from voicebox.jobs.desk import sweep

    s = scribe(tmp_path)
    await start(s, settings(tmp_path), request(media(tmp_path)))
    job = s.jobs.recent()[0]
    s.jobs.write(replace(job, worker_pid=9999))
    sweep(s)
    assert s.jobs.read(job.job_id).state == CRASHED
    assert s.lock().owner() is None


def synthesis(tmp_path, **overrides: object):
    from voicebox.jobs.store import SynthesisJob

    fields = dict(
        job_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        state=RUNNING,
        source=str(tmp_path / "notes.md"),
        output=str(tmp_path / "notes.m4a"),
        format="m4a",
        nemo_binary="nemo-speech",
        claimed_by=ME,
        created_at=1000.0,
        worker_pid=WORKER,
        voice="John",
        tts_model="magpie",
        input_kind="file",
        input_format="markdown",
        chars=6000,
        words=1000,
        paragraphs=10,
        cold_start=False,
        chunks=10,
        chunks_done=4,
    )
    return SynthesisJob(**{**fields, **overrides})


def test_a_running_synthesis_reports_its_chunks(tmp_path):
    s = replace(scribe(tmp_path, alive=(ME, WORKER)), clock=lambda: 1040.0)
    # 2,400 characters in 40 s of synthesis: 3,600 more at 60 a second
    s.jobs.write(synthesis(tmp_path, chars_done=2400, synth_ms=40_000))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply == "running: notes.md, chunk 4 of 10 done, 40 s elapsed, about 60 s left"


def test_pasted_text_is_named_as_such(tmp_path):
    s = replace(scribe(tmp_path, alive=(ME, WORKER)), clock=lambda: 1040.0)
    s.jobs.write(synthesis(tmp_path, source="", chunks=None, chunks_done=0))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply.startswith("running: pasted text, preparing the text")


def test_a_finished_synthesis_names_its_file_length_and_size(tmp_path):
    s = scribe(tmp_path)
    s.jobs.write(
        synthesis(
            tmp_path, state=DONE, audio_ms=754_000, output_bytes=12_400_000, finished_at=2000.0
        )
    )
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply == f"done: notes.md -> {tmp_path / 'notes.m4a'} (13 min of audio, 12.4 MB)"


def test_a_crashed_synthesis_says_how_far_it_got(tmp_path):
    s = scribe(tmp_path)  # WORKER is not alive here
    s.jobs.write(synthesis(tmp_path))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply.startswith("crashed: notes.md: the worker stopped without recording an outcome")
    assert "(after chunk 4 of 10)" in reply


def test_a_slow_machines_synthesis_is_judged_by_the_pace_it_has_shown(tmp_path):
    # 6,000 characters at 1 a second: 100 min, so 30 min in is on time...
    s = replace(scribe(tmp_path, alive=(ME, WORKER)), clock=lambda: 1000.0 + 1800)
    s.jobs.write(synthesis(tmp_path, chars_done=1800, synth_ms=1_800_000))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply == "running: notes.md, chunk 4 of 10 done, 30 min elapsed, about 70 min left"
    # ...and four times the estimate is not
    s = replace(s, clock=lambda: 1000.0 + 4 * 6000 + 60)
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert "it may be wedged" in reply


def test_a_first_request_is_not_called_wedged_before_its_own_timeout(tmp_path):
    from voicebox.synthesize.limits import (
        COLD_START_S,
        FIRST_REQUEST_CEILING_S,
        WARMUP_TIMEOUT_S,
    )

    # the worker may wait out a cold start, then the first request's own timeout
    limit = COLD_START_S + WARMUP_TIMEOUT_S + FIRST_REQUEST_CEILING_S
    s = replace(scribe(tmp_path, alive=(ME, WORKER)), clock=lambda: 1000.0 + limit - 1)
    s.jobs.write(synthesis(tmp_path, chunks_done=0))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply.startswith("running: notes.md, chunk 0 of 10 done, 46 min elapsed")
    s = replace(s, clock=lambda: 1000.0 + limit + 1)
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert "it may be wedged" in reply


def test_time_left_never_reads_zero_while_the_job_runs(tmp_path):
    # far past the up-front estimate with nothing finished yet (a CPU-only first request)
    s = replace(scribe(tmp_path, alive=(ME, WORKER)), clock=lambda: 1000.0 + 1200)
    s.jobs.write(synthesis(tmp_path, chunks_done=0))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply.endswith("about 3 min left")  # 6,000 characters at the measured 29.5/s
    # every chunk spoken, the file still being joined and written
    s.jobs.write(synthesis(tmp_path, chunks_done=10, chars_done=6000, synth_ms=1_100_000))
    reply = status(s, settings(tmp_path), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert reply.endswith("about 1 s left")
