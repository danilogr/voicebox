"""Every synthesis ends in exactly one row the server accepts: ok, failed (reason and
stage), refused, or crashed - the last recorded once however many sessions find it."""

from dataclasses import replace

import pytest
from speak_core.engines.asr import Liveness
from telemetry_support import assert_shippable, opened, ref

from synthesis_support import CLAIMER, JOB_ID, WORKER, FakeSynthesizer, Liveliness, staged
from voicebox.config import Settings
from voicebox.jobs.desk import Desk, reconcile
from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.status import job_status
from voicebox.jobs.store import JobStore
from voicebox.synthesize.worker import WorkerSpeech, run_job
from voicebox.telemetry.jobs import record_job


async def finish(state_dir, synthesizer):
    return await run_job(
        state_dir,
        JOB_ID,
        speech=WorkerSpeech(synthesizer, Liveliness((Liveness.READY,)), None),
        is_alive=lambda pid: True,
        pid=WORKER,
    )


async def test_a_finished_synthesis_is_one_ok_row_with_its_shape(tmp_path):
    db = tmp_path / "telemetry.db"
    opened(db).close()
    staged(tmp_path, "One.\n\nTwo.", voice="My Voice", telemetry=ref(db), created_monotonic=None)
    await finish(tmp_path, FakeSynthesizer())
    (row,) = assert_shippable(db)
    assert (row["kind"], row["status"], row["model"], row["voice"]) == (
        "synthesis",
        "ok",
        "magpie",
        "custom",
    )
    assert (row["chunks"], row["chunks_done"], row["paragraphs"], row["format"]) == (2, 2, 2, "wav")
    assert row["server_warm"] is True and row["output_bytes"] > 0


async def test_a_failed_synthesis_is_failed_at_synthesize_with_chunks_done(tmp_path):
    db = tmp_path / "telemetry.db"
    opened(db).close()
    staged(tmp_path, "One.\n\nTwo.\n\nThree.", telemetry=ref(db))
    await finish(tmp_path, FakeSynthesizer(fail_at=2))
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"], row["stage"], row["chunks_done"]) == (
        "failed",
        "timeout",
        "synthesize",
        1,
    )


async def test_a_worker_that_lost_the_lock_race_is_one_refused_row(tmp_path):
    db = tmp_path / "telemetry.db"
    opened(db).close()
    staged(tmp_path, "One.", telemetry=ref(db))
    job_lock(tmp_path, is_alive=lambda pid: True, pid=300).take_over(CLAIMER)
    with pytest.raises(JobBusy):
        await finish(tmp_path, FakeSynthesizer())
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"]) == ("refused", "busy") and "stage" not in row


def test_a_dead_synthesis_worker_is_crashed_once_and_cleaned_up(tmp_path):
    """Review Focus 3."""
    db = tmp_path / "telemetry.db"
    opened(db).close()
    job = staged(tmp_path, "One.\n\nTwo.", telemetry=ref(db), worker_pid=9999)
    jobs = JobStore(tmp_path)
    jobs.write(replace(job, stage="synthesize", chunks=5, chunks_done=2))
    jobs.work_dir(JOB_ID).mkdir()
    (jobs.work_dir(JOB_ID) / "000000.wav").write_bytes(b"RIFF")

    def desk(pid: int) -> Desk:
        return Desk(
            jobs=jobs,
            lock=lambda: job_lock(tmp_path, is_alive=lambda p: False, pid=pid),
            is_alive=lambda p: False,
            spawn=None,  # type: ignore[arg-type]
            probe=None,  # type: ignore[arg-type]
            clock=lambda: 2000.0,
            pid=pid,
            record=record_job,
        )

    stale = jobs.read(JOB_ID)
    assert stale is not None
    reconcile(desk(1), stale)
    reply = job_status(desk(100), Settings(state_dir=tmp_path), JOB_ID)
    reconcile(desk(2), stale)  # a second session holding the same stale record

    assert reply.startswith("crashed: pasted text:") and "(after chunk 2 of 5)" in reply
    (row,) = assert_shippable(db)
    assert (row["status"], row["stage"], row["chunks_done"]) == ("crashed", "synthesize", 2)
    assert not jobs.text_path(JOB_ID).exists() and not jobs.work_dir(JOB_ID).exists()
    assert job_lock(tmp_path, is_alive=lambda p: False).owner() is None


async def test_every_refusal_reason_is_a_shippable_row(tmp_path):
    from telemetry_support import INSTALL, SESSION

    from synthesis_support import desk_at, request, settings, speech
    from voicebox.synthesize.service import start
    from voicebox.telemetry.session import TelemetrySession

    db = tmp_path / "telemetry.db"
    session = TelemetrySession(conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db)
    taken = tmp_path / "taken.wav"
    taken.write_bytes(b"x")
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"\x00")
    cases = [
        (request(path=str(tmp_path / "gone.md")), speech()),
        (request(path=str(binary)), speech()),
        (request(text="Hi.", output=str(taken)), speech()),
        (
            request(text="Hi.", output=str(tmp_path / "o.m4a"), format="m4a"),
            speech(have=("nemo-speech",)),
        ),
        (request(text="Hi.", output=str(tmp_path / "o.wav")), speech(Liveness.ABSENT, have=())),
        (request(text="Hi.", output=str(tmp_path / "o.wav"), voice="Nobody"), speech()),
    ]
    for req, sp in cases:
        await start(desk_at(tmp_path), sp, settings(tmp_path), req, session)
    reasons = [row["reason"] for row in assert_shippable(db)]
    assert sorted(reasons) == sorted(
        [
            "input_missing",
            "input_unsupported",
            "output_unwritable",
            "ffmpeg_missing",
            "nemo_missing",
            "input_unsupported",
        ]
    )
