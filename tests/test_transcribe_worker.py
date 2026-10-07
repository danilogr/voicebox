from pathlib import Path

import pytest

from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import DONE, EMPTY, FAILED, RUNNING, JobStore, TranscriptionJob
from voicebox.transcribe.commands import CommandResult
from voicebox.transcribe.worker import cli_argv, run_job

CLAIMER, WORKER, OTHER = 100, 200, 300


def alive(pid: int) -> bool:
    return pid in (CLAIMER, WORKER, OTHER)


def start(tmp_path: Path, **overrides: object) -> tuple[JobStore, TranscriptionJob]:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"RIFF")
    fields = dict(
        job_id="j1",
        state=RUNNING,
        source=str(source),
        output=str(tmp_path / "meeting.txt"),
        format="text",
        diarize=True,
        language=None,
        translate_to=None,
        duration_s=63.0,
        size_bytes=4,
        codec="pcm_s16le",
        convert=False,
        asr_model="nemotron-3.5",
        nemo_binary="nemo-speech",
        claimed_by=CLAIMER,
        created_at=1000.0,
    )
    job = TranscriptionJob(**{**fields, **overrides})
    store = JobStore(tmp_path)
    store.write(job)
    job_lock(tmp_path, is_alive=alive, pid=CLAIMER).acquire()
    return store, job


def cli_writing(text: str, code: int = 0):
    calls = []

    async def run_cli(argv, log) -> int:
        calls.append(list(argv))
        output = Path(argv[argv.index("-o") + 1])
        if code == 0:
            output.write_text(text)
        else:
            log.write_text("error: could not load model nemotron-3.5\n")
        return code

    return run_cli, calls


async def run(tmp_path, run_cli, recorded, **kw: object):
    ticks = iter(range(1000, 2000, 5))
    return await run_job(
        tmp_path,
        "j1",
        run_cli=run_cli,
        is_alive=alive,
        clock=lambda: float(next(ticks)),
        pid=WORKER,
        record=recorded.append,
        **kw,
    )


async def test_a_finished_job_is_done_counted_and_recorded(tmp_path):
    store, _ = start(tmp_path)
    run_cli, _ = cli_writing("hello there general kenobi")
    recorded = []
    finished = await run(tmp_path, run_cli, recorded)
    assert finished.state == DONE
    assert (finished.words, finished.chars) == (4, 26)
    assert store.read("j1").state == DONE
    assert [job.state for job in recorded] == [DONE]


async def test_an_empty_transcript_is_empty_not_done(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("   \n")
    recorded = []
    finished = await run(tmp_path, run_cli, recorded)
    assert finished.state == EMPTY
    assert [job.state for job in recorded] == [EMPTY]


async def test_a_failing_cli_fails_the_job_with_its_log(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("", code=1)
    recorded = []
    finished = await run(tmp_path, run_cli, recorded)
    assert finished.state == FAILED
    assert "could not load model" in finished.error
    assert [job.state for job in recorded] == [FAILED]


async def test_the_worker_takes_the_lock_over_and_releases_it(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("words")
    await run(tmp_path, run_cli, [])
    assert job_lock(tmp_path, is_alive=alive).owner() is None


async def test_a_worker_refused_the_lock_records_the_failure_rather_than_dying_quietly(tmp_path):
    """Its stderr is the job log and nobody waits on its exit code, so a worker that
    cannot have the lock has to leave the answer on the record itself."""
    store, _ = start(tmp_path)
    job_lock(tmp_path, is_alive=alive, pid=OTHER).take_over(CLAIMER)
    run_cli, calls = cli_writing("words")
    recorded = []

    with pytest.raises(JobBusy):
        await run(tmp_path, run_cli, recorded)

    refused = store.read("j1")
    assert refused.state == FAILED and "one job at a time" in refused.error
    assert [job.state for job in recorded] == [FAILED]
    assert calls == []
    assert job_lock(tmp_path, is_alive=alive).owner() == OTHER


async def test_conversion_runs_first_and_its_wav_is_deleted(tmp_path):
    store, _ = start(tmp_path, convert=True, codec="flac")
    seen = []

    async def ffmpeg(argv) -> CommandResult:
        seen.append(list(argv))
        Path(argv[-1]).write_bytes(b"RIFF")
        return CommandResult(0, b"", b"")

    run_cli, calls = cli_writing("words")
    await run(tmp_path, run_cli, [], run=ffmpeg)
    assert seen[0][0] == "ffmpeg"
    assert calls[0][3] == str(store.wav_path("j1"))
    assert not store.wav_path("j1").exists()


async def test_a_failed_conversion_fails_the_job_without_running_the_model(tmp_path):
    start(tmp_path, convert=True, codec="flac")

    async def ffmpeg(argv) -> CommandResult:
        return CommandResult(1, b"", b"moov atom not found")

    run_cli, calls = cli_writing("words")
    finished = await run(tmp_path, run_cli, [], run=ffmpeg)
    assert finished.state == FAILED and "moov atom" in finished.error
    assert calls == []


async def test_a_missing_record_is_nothing_to_do(tmp_path):
    assert await run_job(tmp_path, "nope", pid=WORKER, record=lambda job: None) is None


async def test_the_outcome_is_recorded_before_the_lock_is_freed(tmp_path):
    store, _ = start(tmp_path)
    run_cli, _ = cli_writing("words")
    seen = []

    def record(job) -> None:
        seen.append(
            (
                job_lock(tmp_path, is_alive=alive).owner(),
                store.read("j1").state,
            )
        )

    ticks = iter(range(1000, 2000, 5))
    await run_job(
        tmp_path,
        "j1",
        run_cli=run_cli,
        is_alive=alive,
        clock=lambda: float(next(ticks)),
        pid=WORKER,
        record=record,
    )
    assert seen == [(WORKER, DONE)]
    assert job_lock(tmp_path, is_alive=alive).owner() is None


def test_the_cli_is_asked_for_everything_the_caller_chose(tmp_path):
    _, job = start(tmp_path, format="srt", language="pt", translate_to="en")
    argv = cli_argv(job, Path("/x/in.wav"))
    assert argv[:4] == ["nemo-speech", "--quiet", "transcribe", "/x/in.wav"]
    assert argv[4:6] == ["--model", "nemotron-3.5"]
    assert "--diarize" in argv
    assert argv[argv.index("-f") + 1] == "srt"
    assert argv[argv.index("--language") + 1] == "pt"
    assert argv[argv.index("--translate-to") + 1] == "en"
