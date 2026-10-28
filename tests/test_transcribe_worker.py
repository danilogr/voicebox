import json
from pathlib import Path

import pytest

from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import (
    DONE,
    EMPTY,
    FAILED,
    QUEUED,
    RUNNING,
    JobStore,
    TranscriptionJob,
)
from voicebox.programs import CommandResult
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


async def test_a_worker_refused_the_lock_puts_its_job_back_in_the_queue(tmp_path):
    """Another job took the lock between this one's start and its takeover: this job
    waits its turn again instead of failing as busy."""
    store, _ = start(tmp_path)
    job_lock(tmp_path, is_alive=alive, pid=OTHER).take_over(CLAIMER)
    run_cli, calls = cli_writing("words")
    recorded = []

    with pytest.raises(JobBusy):
        await run(tmp_path, run_cli, recorded)

    assert store.read("j1").state == QUEUED
    assert recorded == []  # it has not ended, so it has no row yet
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
    argv = cli_argv(job, Path("/x/in.wav"), Path(job.output))
    assert argv[:4] == ["nemo-speech", "--quiet", "transcribe", "/x/in.wav"]
    assert argv[4:6] == ["--model", "nemotron-3.5"]
    assert "--diarize" in argv
    assert argv[argv.index("-f") + 1] == "srt"
    assert argv[argv.index("--language") + 1] == "pt"
    assert argv[argv.index("--translate-to") + 1] == "en"


def test_json_with_speakers_asks_the_recogniser_for_no_speakers(tmp_path):
    """`transcribe --diarize` drops speakers past ~20 minutes, so a json transcript's
    speakers come from the diarizer instead and the recogniser is not asked for them."""
    _, job = start(tmp_path, format="json")
    assert "--diarize" not in cli_argv(job, Path("/x/in.wav"), Path(job.output))


def test_text_formats_still_ask_the_recogniser_for_speakers(tmp_path):
    _, job = start(tmp_path, format="text")
    assert "--diarize" in cli_argv(job, Path("/x/in.wav"), Path(job.output))


UNTAGGED_JSON = json.dumps(
    {
        "text": "hi there",
        "words": [
            {"word": "hi", "start": 0.2, "end": 0.4, "confidence": 1},
            {"word": "there", "start": 1300.0, "end": 1300.3, "confidence": 1},
        ],
    }
)
SEGMENTS_JSON = json.dumps(
    {
        "segments": [
            {"start": 0.0, "end": 1.0, "speaker": 2},
            {"start": 1299.5, "end": 1301.0, "speaker": 1},
        ]
    }
)


def nemo_writing(diarize_code: int = 0, transcript: str = UNTAGGED_JSON, segments=SEGMENTS_JSON):
    """A fake `nemo-speech` that answers `transcribe` and `diarize` like the real one, and
    notes which files exist each time it runs."""
    calls, files = [], []

    async def run_cli(argv, log) -> int:
        calls.append(list(argv))
        files.append(sorted(path.name for path in log.parent.iterdir()))
        output = Path(argv[argv.index("-o") + 1])
        if argv[2] == "transcribe":
            output.write_text(transcript)
            return 0
        if diarize_code == 0:
            output.write_text(segments)
        else:
            log.write_text("error: could not load model nemotron-3-diarization\n")
        return diarize_code

    run_cli.files = files
    return run_cli, calls


def json_job(tmp_path, **overrides: object) -> tuple[JobStore, TranscriptionJob]:
    return start(tmp_path, format="json", output=str(tmp_path / "meeting.json"), **overrides)


async def test_json_words_take_their_speakers_from_the_diarizer(tmp_path):
    store, job = json_job(tmp_path)
    run_cli, calls = nemo_writing()

    finished = await run(tmp_path, run_cli, [])

    assert finished.state == DONE
    assert [call[2] for call in calls] == ["transcribe", "diarize"]
    assert calls[1][3] == job.source
    words = json.loads(Path(job.output).read_text())["words"]
    assert [w["speaker"] for w in words] == [2, 1]
    assert not store.diarization_path("j1").exists()
    assert not store.untagged_path("j1").exists()


async def test_the_untagged_transcript_is_never_where_the_finished_one_goes(tmp_path):
    """While the diarizer runs, the transcript without speakers waits among the job's
    files, so a job cancelled or crashed then leaves nothing that looks finished."""
    store, _ = json_job(tmp_path)
    run_cli, calls = nemo_writing()

    await run(tmp_path, run_cli, [])

    transcribe, diarize = (call[call.index("-o") + 1] for call in calls)
    assert transcribe == str(store.untagged_path("j1"))
    assert diarize == str(store.diarization_path("j1"))
    assert "meeting.json" not in run_cli.files[1]  # the output, while diarizing
    assert "j1.untagged" in run_cli.files[1]


async def test_converted_audio_is_what_the_diarizer_hears(tmp_path):
    store, _ = json_job(tmp_path, convert=True, codec="aac")

    async def ffmpeg(argv) -> CommandResult:
        Path(argv[-1]).write_bytes(b"RIFF")
        return CommandResult(0, b"", b"")

    run_cli, calls = nemo_writing()
    await run(tmp_path, run_cli, [], run=ffmpeg)
    assert calls[1][2:4] == ["diarize", str(store.wav_path("j1"))]


async def test_a_failing_diarizer_fails_the_job_and_leaves_no_transcript(tmp_path):
    """A transcript without the speakers it was asked for is not the finished one, so
    it is not left where the finished one would be."""
    store, job = json_job(tmp_path)
    run_cli, _ = nemo_writing(diarize_code=1)

    finished = await run(tmp_path, run_cli, [])

    assert finished.state == FAILED
    assert finished.reason == "model_failed"
    assert "nemotron-3-diarization" in finished.error
    assert not Path(job.output).exists()
    assert not store.untagged_path("j1").exists()


@pytest.mark.parametrize("segments", ['{"segments": [', '{"segments": null}', '{"file": "x"}'])
async def test_unreadable_diarizer_output_fails_the_job_as_the_model(tmp_path, segments):
    """The diarizer exiting 0 with segments no one can read is the model failing, not a
    bug in the worker, and leaves no transcript and no half-written file behind."""
    _, job = json_job(tmp_path)
    run_cli, _ = nemo_writing(segments=segments)

    finished = await run(tmp_path, run_cli, [])

    assert finished.state == FAILED
    assert finished.reason == "model_failed"
    assert "could not tag the speakers" in finished.error
    assert not Path(job.output).exists()
    assert not list(tmp_path.glob(".meeting.json*"))


async def test_with_nothing_heard_no_diarizer_runs(tmp_path):
    """A silent recording has no one to tell apart: it ends empty without the diarizer's
    time, its transcript written as the recogniser wrote it."""
    silent = json.dumps({"text": "", "words": []})
    _, job = json_job(tmp_path)
    run_cli, calls = nemo_writing(transcript=silent)

    finished = await run(tmp_path, run_cli, [])

    assert finished.state == EMPTY
    assert [call[2] for call in calls] == ["transcribe"]
    assert json.loads(Path(job.output).read_text()) == json.loads(silent)


async def test_json_without_speakers_runs_no_diarizer(tmp_path):
    _, job = json_job(tmp_path, diarize=False)
    run_cli, calls = nemo_writing()
    await run(tmp_path, run_cli, [])
    assert [call[2] for call in calls] == ["transcribe"]
    assert "speaker" not in json.loads(Path(job.output).read_text())["words"][0]
