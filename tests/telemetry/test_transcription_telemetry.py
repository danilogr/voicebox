"""Every transcription ends in exactly one row: the worker records how it finished, the
tool records a refusal, and whoever notices a dead worker records the crash."""

from dataclasses import replace
from pathlib import Path

import pytest
from telemetry_support import INSTALL, SESSION, assert_shippable, opened, ref, stored_rows

from fakes import installed
from voicebox.config import Settings
from voicebox.jobs.desk import Desk, claim, reconcile
from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import RUNNING, JobStore, TranscriptionJob
from voicebox.programs import CommandResult
from voicebox.telemetry.jobs import record_job
from voicebox.telemetry.session import TelemetrySession
from voicebox.transcribe.probe import Probe
from voicebox.transcribe.service import TranscribeRequest, start
from voicebox.transcribe.worker import run_job

CLAIMER, WORKER = 100, 200
AUDIO = Probe(63.0, 4096, "wav", "pcm_s16le", 16000, 1)


def job(tmp_path: Path, db: Path, **overrides: object) -> TranscriptionJob:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"RIFF")
    fields = dict(
        job_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        state=RUNNING,
        source=str(source),
        output=str(tmp_path / "meeting.txt"),
        format="text",
        nemo_binary="nemo-speech",
        claimed_by=CLAIMER,
        created_at=1000.0,
        created_monotonic=None,
        stage="asr",
        telemetry=ref(db),
        diarize=True,
        language=None,
        translate_to=None,
        duration_s=63.0,
        size_bytes=4096,
        codec="pcm_s16le",
        convert=False,
        asr_model="nemotron-3.5",
        has_video=True,
    )
    return TranscriptionJob(**{**fields, **overrides})


def started(tmp_path: Path, **overrides: object) -> Path:
    db = tmp_path / "telemetry.db"
    opened(db).close()
    JobStore(tmp_path).write(job(tmp_path, db, **overrides))
    job_lock(tmp_path, is_alive=lambda pid: True, pid=CLAIMER).acquire()
    return db


def cli(code: int = 0, text: str = "hello there", missing: bool = False):
    async def run_cli(argv, log) -> int:
        if missing:
            raise FileNotFoundError(argv[0])
        if code == 0:
            Path(argv[argv.index("-o") + 1]).write_text(text)
        return code

    return run_cli


async def finish(tmp_path: Path, **kw: object):
    ticks = iter(range(1000, 2000, 5))
    return await run_job(
        tmp_path,
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        is_alive=lambda pid: pid in (CLAIMER, WORKER),
        clock=lambda: float(next(ticks)),
        pid=WORKER,
        record=record_job,
        **kw,
    )


async def test_a_finished_transcription_is_one_ok_row_with_its_shape(tmp_path):
    db = started(tmp_path)
    await finish(tmp_path, run_cli=cli())
    (row,) = assert_shippable(db)
    assert (row["kind"], row["status"], row["engine"], row["model"]) == (
        "transcription",
        "ok",
        "nemo",
        "nemotron-3.5",
    )
    assert (row["audio_ms"], row["source_bytes"], row["audio_codec"]) == (63_000, 4096, "pcm_s16le")
    assert (row["has_video"], row["converted"], row["diarize"], row["translate"]) == (
        True,
        False,
        True,
        False,
    )
    assert (row["language"], row["format"], row["words"], row["chars"]) == ("auto", "text", 2, 11)
    assert "reason" not in row and "stage" not in row


async def test_the_local_columns_hold_the_names_and_never_ship(tmp_path):
    db = started(tmp_path)
    await finish(tmp_path, run_cli=cli())
    (stored,) = stored_rows(db)
    assert stored["source_name"] == "meeting.wav"
    assert stored["output_path"] == str(tmp_path / "meeting.txt")
    (shipped,) = assert_shippable(db)
    assert not {"source_name", "output_path", "error_message"} & set(shipped)


async def test_an_empty_transcript_is_an_empty_row(tmp_path):
    db = started(tmp_path)
    await finish(tmp_path, run_cli=cli(text="  "))
    assert [r["status"] for r in assert_shippable(db)] == ["empty"]


async def test_a_model_failure_is_failed_at_asr(tmp_path):
    db = started(tmp_path)
    await finish(tmp_path, run_cli=cli(code=1))
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"], row["stage"]) == ("failed", "model_failed", "asr")


async def test_a_missing_nemo_is_its_own_reason(tmp_path):
    db = started(tmp_path)
    await finish(tmp_path, run_cli=cli(missing=True))
    (row,) = assert_shippable(db)
    assert (row["reason"], row["stage"]) == ("nemo_missing", "asr")


async def test_a_failed_conversion_is_failed_at_convert(tmp_path):
    db = started(tmp_path, convert=True, stage="convert")

    async def ffmpeg(argv) -> CommandResult:
        return CommandResult(1, b"", b"Invalid data found")

    await finish(tmp_path, run_cli=cli(), run=ffmpeg)
    (row,) = assert_shippable(db)
    assert (row["reason"], row["stage"], row["converted"]) == ("conversion_failed", "convert", True)


async def test_a_conversion_that_timed_out_is_a_timeout(tmp_path):
    db = started(tmp_path, convert=True, stage="convert")

    async def stalled(argv) -> CommandResult:
        return CommandResult(-1, b"", b"ffmpeg timed out after 1800s", timed_out=True)

    await finish(tmp_path, run_cli=cli(), run=stalled)
    assert assert_shippable(db)[0]["reason"] == "timeout"


async def test_conversion_time_is_measured(tmp_path):
    db = started(tmp_path, convert=True, stage="convert")

    async def ffmpeg(argv) -> CommandResult:
        Path(argv[-1]).write_bytes(b"RIFF")
        return CommandResult(0, b"", b"")

    await finish(tmp_path, run_cli=cli(), run=ffmpeg)
    (row,) = assert_shippable(db)
    assert isinstance(row["convert_ms"], int) and row["convert_ms"] > 0


async def test_a_model_failure_after_a_conversion_is_failed_at_asr_and_counts_the_conversion(
    tmp_path,
):
    db = started(tmp_path, convert=True, stage="convert")

    async def ffmpeg(argv) -> CommandResult:
        Path(argv[-1]).write_bytes(b"RIFF")
        return CommandResult(0, b"", b"")

    await finish(tmp_path, run_cli=cli(code=1), run=ffmpeg)
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"], row["stage"]) == ("failed", "model_failed", "asr")
    assert row["converted"] is True and row["convert_ms"] > 0


async def test_a_worker_that_loses_the_lock_race_is_refused_busy(tmp_path):
    db = started(tmp_path)
    job_lock(tmp_path, is_alive=lambda pid: True, pid=300).take_over(CLAIMER)
    with pytest.raises(JobBusy):
        await run_job(
            tmp_path,
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            is_alive=lambda pid: True,
            pid=WORKER,
            record=record_job,
            run_cli=cli(),
        )
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"]) == ("refused", "busy")
    assert "stage" not in row
    # The worker never ran, so nothing was converted whatever the plan said.
    assert "converted" not in row


def test_a_dead_worker_is_recorded_as_crashed_once_at_its_stage(tmp_path):
    db = started(tmp_path, stage="convert", worker_pid=9999)

    def desk(pid: int) -> Desk:
        return Desk(
            jobs=JobStore(tmp_path),
            lock=lambda: job_lock(tmp_path, is_alive=lambda p: False, pid=pid),
            is_alive=lambda p: False,
            spawn=None,  # type: ignore[arg-type]
            probe=None,  # type: ignore[arg-type]
            clock=lambda: 2000.0,
            record=record_job,
        )

    stale = JobStore(tmp_path).read("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert stale is not None
    reconcile(desk(1), stale)
    reconcile(desk(2), stale)  # a second session that read the record before the first wrote
    (row,) = assert_shippable(db)
    assert (row["status"], row["stage"]) == ("crashed", "convert")
    assert "reason" not in row


def test_a_crash_found_after_its_session_ended_and_shipped_still_records(tmp_path):
    """The session row stays as long as any job may name it - core never deletes a
    session, and the backlog cap prunes rows, not sessions - so a crash noticed days
    later is stored under the session that asked for the job."""
    from speak_core.telemetry import store

    from voicebox.telemetry.contract import JOBS, SCHEMA

    db = started(tmp_path, worker_pid=9999)
    conn = store.connect(db, SCHEMA)
    store.end_session(conn, SESSION, "2026-10-06T11:00:00Z")
    store.mark_sessions_shipped(
        conn, store.unshipped_sessions(conn, INSTALL), "2026-10-06T11:00:01Z"
    )
    for n in range(3):  # a backlog cap of 1 prunes older rows, never the session
        store.insert_row(
            conn, JOBS, {**stored_row_template(n), "session_id": SESSION}, backlog_cap=1
        )
    conn.close()
    stale = JobStore(tmp_path).read("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert stale is not None
    record_job(replace(stale, state="crashed", finished_at=2000.0))
    crashed = [r for r in stored_rows(db) if r["status"] == "crashed"]
    assert [r["session_id"] for r in crashed] == [SESSION]


def stored_row_template(n: int) -> dict[str, object]:
    from voicebox.telemetry.rows import TRANSCRIPTION, JobFacts, iso, job_row

    return job_row(
        JobFacts(
            job_id=f"dddddddd-dddd-4ddd-8ddd-{n:012d}",
            session_id=SESSION,
            install_id=INSTALL,
            kind=TRANSCRIPTION,
            started_at=iso(1_790_000_000 + n),
            ended_at=iso(1_790_000_001 + n),
            wall_ms=1000,
            status="ok",
            engine="nemo",
        )
    )


def test_a_job_whose_session_records_nothing_leaves_no_row(tmp_path):
    db = started(tmp_path, telemetry=None)
    record_job(
        replace(JobStore(tmp_path).read("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), state="done")
    )
    assert stored_rows(db) == []


def session_for(tmp_path: Path) -> TelemetrySession:
    db = tmp_path / "telemetry.db"
    return TelemetrySession(conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db)


def desk_for(tmp_path: Path, found: Probe | Exception = AUDIO, alive=(CLAIMER,)) -> Desk:
    async def probe(path) -> Probe:
        if isinstance(found, Exception):
            raise found
        return found

    async def spawn(argv, log) -> int:
        return WORKER

    return Desk(
        jobs=JobStore(tmp_path / "state"),
        lock=lambda: job_lock(tmp_path / "state", is_alive=lambda p: p in alive, pid=CLAIMER),
        is_alive=lambda p: p in alive,
        spawn=spawn,
        probe=probe,
        which=installed,
        pid=CLAIMER,
    )


def request(path: Path, **overrides: object) -> TranscribeRequest:
    fields = dict(
        path=str(path),
        output_dir=None,
        format="srt",
        diarize=False,
        language="pt",
        translate_to="en",
    )
    return TranscribeRequest(**{**fields, **overrides})


async def refused(tmp_path: Path, desk: Desk, req: TranscribeRequest) -> dict[str, object]:
    session = session_for(tmp_path)
    await start(desk, Settings(state_dir=tmp_path / "state"), req, session)
    (row,) = assert_shippable(session.db_path)
    assert row["status"] == "refused" and "stage" not in row
    return row


async def test_a_missing_file_is_refused_input_missing(tmp_path):
    row = await refused(tmp_path, desk_for(tmp_path), request(tmp_path / "gone.m4a"))
    assert row["reason"] == "input_missing"


async def test_a_file_with_no_audio_is_refused_input_unsupported_with_its_shape(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    video_only = Probe(14.4, 408_576, "mov,mp4", None, None, None, has_video=True)
    row = await refused(tmp_path, desk_for(tmp_path, video_only), request(media))
    assert (row["reason"], row["has_video"], row["source_bytes"]) == (
        "input_unsupported",
        True,
        408_576,
    )
    assert (row["language"], row["translate"], row["format"], row["diarize"]) == (
        "pt",
        True,
        "srt",
        False,
    )


async def test_missing_ffprobe_is_refused_ffmpeg_missing(tmp_path):
    from voicebox.programs import ToolMissing

    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    row = await refused(tmp_path, desk_for(tmp_path, ToolMissing("ffprobe", "x")), request(media))
    assert row["reason"] == "ffmpeg_missing"


async def test_a_missing_nemo_speech_is_refused_nemo_missing(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    desk = replace(desk_for(tmp_path), which=lambda name: None)
    row = await refused(tmp_path, desk, request(media))
    assert (row["reason"], row["source_bytes"]) == ("nemo_missing", AUDIO.size_bytes)


async def test_a_relative_output_directory_is_refused_output_unwritable(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    row = await refused(tmp_path, desk_for(tmp_path), request(media, output_dir="out"))
    assert row["reason"] == "output_unwritable"


async def test_a_path_whose_home_cannot_be_found_is_refused_input_missing(tmp_path):
    req = request(Path("~no-such-user-voicebox/a.m4a"))
    row = await refused(tmp_path, desk_for(tmp_path), req)
    assert row["reason"] == "input_missing"


async def test_a_huge_file_refused_after_its_probe_is_clamped(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    huge = Probe(200_000.0, 20 * 1024**3, "wav", "pcm_s16le", 16000, 1)
    row = await refused(tmp_path, desk_for(tmp_path, huge), request(media, output_dir="rel"))
    assert (row["reason"], row["source_bytes"], row["audio_ms"]) == (
        "output_unwritable",
        10_737_418_240,
        86_400_000,
    )


async def test_a_job_sent_while_another_runs_waits_and_records_nothing_yet(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    session = session_for(tmp_path)
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=300).acquire()
    desk = desk_for(tmp_path, alive=(CLAIMER, 300))
    reply = await start(desk, Settings(state_dir=tmp_path / "state"), request(media), session)
    assert "queued behind 1 job" in reply
    assert [job.state for job in desk.jobs.recent()] == ["queued"]
    assert stored_rows(session.db_path) == []  # a job is recorded when it ends


async def test_an_accepted_job_carries_its_telemetry_ref_and_first_stage(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    session = session_for(tmp_path)
    desk = desk_for(tmp_path)
    await start(desk, Settings(state_dir=tmp_path / "state"), request(media), session)
    (accepted,) = desk.jobs.recent()
    assert accepted.telemetry == session.ref()
    # AUDIO is 16-bit PCM in a WAV container, so no conversion: the worker starts at asr.
    assert accepted.stage == "asr"
    assert accepted.created_monotonic is not None
    assert stored_rows(session.db_path) == []  # an accepted job is recorded when it ends


async def test_a_worker_that_cannot_be_spawned_leaves_one_failed_other_row(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    session = session_for(tmp_path)

    async def spawn(argv, log) -> int:
        raise OSError("no exec")

    desk = replace(desk_for(tmp_path), spawn=spawn, record=record_job)
    reply = await start(desk, Settings(state_dir=tmp_path / "state"), request(media), session)
    assert "could not start" in reply
    (row,) = assert_shippable(session.db_path)
    assert (row["status"], row["reason"], row["stage"]) == ("failed", "other", "asr")
    assert "converted" not in row


async def test_a_job_record_that_cannot_be_written_is_still_one_failed_other_row(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    session = session_for(tmp_path)
    desk = replace(desk_for(tmp_path), record=record_job)

    def write(job) -> None:
        raise OSError("disk full")

    desk.jobs.write = write  # type: ignore[method-assign]
    reply = await start(desk, Settings(state_dir=tmp_path / "state"), request(media), session)
    assert "could not start" in reply
    (row,) = assert_shippable(session.db_path)
    assert (row["status"], row["reason"]) == ("failed", "other")
    assert "converted" not in row
    # The lock is given back, so the next job is not turned away.
    claim(desk).release()


@pytest.mark.parametrize("seconds", [float("nan"), float("inf")])
def test_a_duration_that_is_not_finite_still_leaves_a_row_without_it(tmp_path, seconds):
    db = started(tmp_path, duration_s=seconds)
    record_job(
        replace(
            JobStore(tmp_path).read("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            state="done",
            finished_at=1100.0,
        )
    )
    (row,) = assert_shippable(db)
    assert row["status"] == "ok" and "audio_ms" not in row


async def test_a_probe_with_a_duration_that_is_not_finite_is_still_one_refusal(tmp_path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"x")
    desk = desk_for(tmp_path, found=Probe(float("inf"), 1, "wav", "pcm_s16le", 16000, 1))
    session = session_for(tmp_path)
    await start(
        desk,
        Settings(state_dir=tmp_path / "state"),
        request(media, output_dir=str(tmp_path / "missing")),
        session,
    )
    (row,) = assert_shippable(session.db_path)
    assert (row["status"], row["reason"]) == ("refused", "output_unwritable")
    assert "audio_ms" not in row


@pytest.mark.parametrize("ref_value", [{"db_path": "x"}, {"db_path": "x", "session_id": 1}])
def test_a_job_whose_telemetry_reference_is_malformed_records_nothing_and_never_raises(
    tmp_path, ref_value
):
    db = tmp_path / "t.db"
    ref_value = {**ref_value, "db_path": str(db)}
    stale = job(tmp_path, db, state="done", finished_at=1100.0)
    record_job(replace(stale, telemetry=ref_value))  # type: ignore[arg-type]
    assert not db.exists() or stored_rows(db) == []


async def test_a_spawn_failure_is_still_recorded_when_writing_the_record_fails(tmp_path):
    media = tmp_path / "a.m4a"
    media.write_bytes(b"x")
    session = session_for(tmp_path)
    recorded = []

    async def spawn(argv, log) -> int:
        raise OSError("no exec")

    desk = replace(desk_for(tmp_path), spawn=spawn, record=recorded.append)
    real = desk.jobs.write
    calls = []

    def write(job) -> None:
        calls.append(job.state)
        if len(calls) > 1:
            raise OSError("disk full")
        real(job)

    desk.jobs.write = write  # type: ignore[method-assign]
    reply = await start(desk, Settings(state_dir=tmp_path / "state"), request(media), session)
    assert "could not start" in reply
    assert [(j.state, j.reason) for j in recorded] == [("failed", "other")]
