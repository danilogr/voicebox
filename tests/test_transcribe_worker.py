from dataclasses import replace
from pathlib import Path

import pytest

from speak.telemetry.session import (
    MAX_ASR_MS,
    MAX_AUDIO_MS,
    MAX_CHARS,
    MAX_SOURCE_BYTES,
    MAX_WORDS,
)
from speak.transcribe.commands import CommandResult
from speak.transcribe.jobs import DONE, EMPTY, FAILED, RUNNING, Job, JobStore
from speak.transcribe.lock import TranscriptionBusy, transcription_lock
from speak.transcribe.worker import cli_argv, outcome_for, run_job, write_telemetry

CLAIMER, WORKER, OTHER = 100, 200, 300


def alive(pid: int) -> bool:
    return pid in (CLAIMER, WORKER, OTHER)


def start(tmp_path: Path, **overrides: object) -> tuple[JobStore, Job]:
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
    job = Job(**{**fields, **overrides})
    store = JobStore(tmp_path)
    store.write(job)
    transcription_lock(tmp_path, is_alive=alive, pid=CLAIMER).acquire()
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
    assert outcome_for(recorded[0]).status == "ok"


async def test_an_empty_transcript_is_empty_not_done(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("   \n")
    recorded = []
    finished = await run(tmp_path, run_cli, recorded)
    assert finished.state == EMPTY
    assert outcome_for(recorded[0]).status == "heard_nothing"


async def test_a_failing_cli_fails_the_job_with_its_log(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("", code=1)
    recorded = []
    finished = await run(tmp_path, run_cli, recorded)
    assert finished.state == FAILED
    assert "could not load model" in finished.error
    assert outcome_for(recorded[0]).status == "failed"


async def test_the_worker_takes_the_lock_over_and_releases_it(tmp_path):
    start(tmp_path)
    run_cli, _ = cli_writing("words")
    await run(tmp_path, run_cli, [])
    assert transcription_lock(tmp_path, is_alive=alive).owner() is None


async def test_a_worker_refused_the_lock_records_the_failure_rather_than_dying_quietly(tmp_path):
    """Its stderr is the job log and nobody waits on its exit code, so a worker that
    cannot have the lock has to leave the answer on the record itself."""
    store, _ = start(tmp_path)
    transcription_lock(tmp_path, is_alive=alive, pid=OTHER).take_over(CLAIMER)
    run_cli, calls = cli_writing("words")
    recorded = []

    with pytest.raises(TranscriptionBusy):
        await run(tmp_path, run_cli, recorded)

    refused = store.read("j1")
    assert refused.state == FAILED and "only one runs at a time" in refused.error
    assert [job.state for job in recorded] == [FAILED]
    assert calls == []
    assert transcription_lock(tmp_path, is_alive=alive).owner() == OTHER


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
                transcription_lock(tmp_path, is_alive=alive).owner(),
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
    assert transcription_lock(tmp_path, is_alive=alive).owner() is None


def test_an_unopenable_telemetry_store_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    job = Job(
        job_id="j1",
        state=DONE,
        source=str(tmp_path / "meeting.wav"),
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
        telemetry={"db_path": str(blocker / "t.db"), "session_id": "s", "install_id": "i"},
    )
    write_telemetry(job)  # must not raise


def test_a_file_past_the_contracts_ceilings_is_recorded_at_them(tmp_path):
    """Nothing upstream bounds a file, and a row above a ceiling comes back 400 - which
    the shipper does not retry, so that chunk and every one after it stop shipping."""
    _, job = start(
        tmp_path,
        size_bytes=20 * 1024**3,  # a 20 GiB video, over the 10 GiB ceiling
        duration_s=200_000.0,  # 55 hours, over the 24 the contract allows
        asr_ms=90_000_000,
        words=600_000,
        chars=6_000_000,
    )
    outcome = outcome_for(replace(job, state=DONE))
    assert outcome.source_bytes == MAX_SOURCE_BYTES
    assert outcome.audio_ms == MAX_AUDIO_MS
    assert outcome.asr_ms == MAX_ASR_MS
    assert outcome.words == MAX_WORDS
    assert outcome.chars == MAX_CHARS


def test_a_path_shaped_codec_is_dropped_rather_than_sent(tmp_path):
    """A malformed probe could hand back anything as `codec_name`; the contract's
    `audio_codec` pattern rejects it with a 400 the shipper never retries, taking
    unrelated speak/listen rows in the same chunk down with it."""
    _, job = start(tmp_path, codec="/etc/passwd")
    outcome = outcome_for(replace(job, state=DONE))
    assert outcome.audio_codec is None


def test_an_ordinary_codec_survives(tmp_path):
    _, job = start(tmp_path, codec="pcm_s16le")
    outcome = outcome_for(replace(job, state=DONE))
    assert outcome.audio_codec == "pcm_s16le"


def test_an_overlong_codec_is_dropped_rather_than_sent(tmp_path):
    """The contract also caps `audio_codec` at 32 characters; a codec that matches
    the pattern but is longer than that would still be rejected by the server with
    the same 400 that ends unrelated speak/listen rows in the same chunk."""
    _, job = start(tmp_path, codec="a" * 33)
    outcome = outcome_for(replace(job, state=DONE))
    assert outcome.audio_codec is None


def test_a_non_string_codec_is_dropped_without_raising(tmp_path):
    """A hand-edited or corrupted job record on disk could hold anything under
    `codec`; the telemetry write must not raise a `TypeError` out of a bad type."""
    _, job = start(tmp_path, codec=12345)
    outcome = outcome_for(replace(job, state=DONE))
    assert outcome.audio_codec is None


def test_the_cli_is_asked_for_everything_the_caller_chose(tmp_path):
    _, job = start(tmp_path, format="srt", language="pt", translate_to="en")
    argv = cli_argv(job, Path("/x/in.wav"))
    assert argv[:4] == ["nemo-speech", "--quiet", "transcribe", "/x/in.wav"]
    assert argv[4:6] == ["--model", "nemotron-3.5"]
    assert "--diarize" in argv
    assert argv[argv.index("-f") + 1] == "srt"
    assert argv[argv.index("--language") + 1] == "pt"
    assert argv[argv.index("--translate-to") + 1] == "en"
