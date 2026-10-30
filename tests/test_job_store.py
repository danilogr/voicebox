import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from voicebox.jobs.store import (
    DONE,
    RETENTION_S,
    RUNNING,
    TRANSCRIPTION,
    JobStore,
    TranscriptionJob,
    eta_seconds,
    new_job_id,
)
from voicebox.transcribe.output import output_path, transcript_text


def job(**overrides: object) -> TranscriptionJob:
    fields = dict(
        job_id="abc123",
        state=RUNNING,
        source="/a/meeting.m4a",
        output="/a/meeting.txt",
        format="text",
        diarize=True,
        language=None,
        translate_to=None,
        duration_s=3780.0,
        size_bytes=49_283_072,
        codec="aac",
        convert=True,
        asr_model="nemotron-3.5",
        nemo_binary="nemo-speech",
        claimed_by=100,
        created_at=1000.0,
    )
    return TranscriptionJob(**{**fields, **overrides})


def test_a_job_round_trips(tmp_path):
    store = JobStore(tmp_path)
    store.write(job())
    assert store.read("abc123") == job()


def test_a_rewrite_replaces_the_record(tmp_path):
    store = JobStore(tmp_path)
    store.write(job())
    store.write(replace(job(), state=DONE, words=9100))
    assert store.read("abc123").state == DONE


def test_a_missing_or_corrupt_record_reads_as_none(tmp_path):
    store = JobStore(tmp_path)
    assert store.read("nope") is None
    store.record_path("bad").parent.mkdir(parents=True, exist_ok=True)
    store.record_path("bad").write_text("{not json")
    assert store.read("bad") is None


def test_recent_jobs_come_newest_first(tmp_path):
    store = JobStore(tmp_path)
    for i, created in enumerate([10.0, 30.0, 20.0]):
        store.write(job(job_id=f"j{i}", created_at=created))
    assert [j.job_id for j in store.recent()] == ["j1", "j2", "j0"]


def test_an_old_terminal_record_is_pruned(tmp_path):
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time() - RETENTION_S - 3600))
    store.log_path("abc123").write_text("log")
    assert store.recent() == []
    assert not store.record_path("abc123").exists()
    assert not store.log_path("abc123").exists()


def test_a_running_job_is_never_pruned_however_old(tmp_path):
    store = JobStore(tmp_path)
    store.write(job(state=RUNNING, created_at=time.time() - RETENTION_S - 3600))
    wav = store.wav_path("abc123")
    wav.write_bytes(b"data")
    assert [j.job_id for j in store.recent()] == ["abc123"]
    assert wav.exists()


def test_a_stray_wav_beside_a_finished_job_is_reclaimed_before_the_retention_window(
    tmp_path,
):
    """Nothing but a status call ever reclaims the converted audio a `SIGKILL`ed
    worker left behind, so this cannot wait for the record's own retention window -
    it is the ~115 MB/hour that makes stranding one costly."""
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time()))
    wav = store.wav_path("abc123")
    wav.write_bytes(b"data")
    store.recent()
    assert not wav.exists()
    assert store.read("abc123") is not None  # the record is still within the window


def test_a_prune_failure_does_not_break_recent(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time() - RETENTION_S - 3600))

    def boom(self: Path, missing_ok: bool = False) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "unlink", boom)
    assert [j.job_id for j in store.recent()] == ["abc123"]


def test_a_non_numeric_finished_at_does_not_break_recent(tmp_path):
    """A hand-edited or corrupted record can hold anything under `finished_at`; the
    subtraction that ages it against `RETENTION_S` must not raise a `TypeError` out
    of a `job_status` call."""
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time()))
    path = store.record_path("abc123")
    data = json.loads(path.read_text())
    data["finished_at"] = "not-a-number"
    path.write_text(json.dumps(data))
    assert [j.job_id for j in store.recent()] == ["abc123"]


def test_the_pid_sidecar_round_trips(tmp_path):
    store = JobStore(tmp_path)
    store.write_pid("abc123", 4242)
    assert store.read_pid("abc123") == 4242


def test_a_missing_pid_sidecar_reads_as_none(tmp_path):
    store = JobStore(tmp_path)
    assert store.read_pid("nope") is None


def test_a_malformed_pid_sidecar_reads_as_none(tmp_path):
    store = JobStore(tmp_path)
    store.pid_path("abc123").parent.mkdir(parents=True, exist_ok=True)
    store.pid_path("abc123").write_text("not-a-pid")
    assert store.read_pid("abc123") is None


def test_the_pid_sidecar_is_pruned_with_the_record_and_log(tmp_path):
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time() - RETENTION_S - 3600))
    store.write_pid("abc123", 4242)
    assert store.recent() == []
    assert not store.pid_path("abc123").exists()


def test_the_pid_sidecar_survives_within_the_retention_window(tmp_path):
    store = JobStore(tmp_path)
    store.write(job(state=DONE, finished_at=time.time()))
    store.write_pid("abc123", 4242)
    store.recent()
    assert store.read_pid("abc123") == 4242


def test_the_log_tail_is_the_last_lines(tmp_path):
    store = JobStore(tmp_path)
    store.log_path("abc123").parent.mkdir(parents=True, exist_ok=True)
    store.log_path("abc123").write_text("\n".join(f"line {i}" for i in range(20)))
    assert store.log_tail("abc123", lines=2) == "line 18\nline 19"


def test_the_estimate_uses_the_measured_ratios():
    assert eta_seconds(1030.0, diarize=True) == pytest.approx(100.0)
    assert eta_seconds(1300.0, diarize=False) == pytest.approx(100.0)


def test_job_ids_are_distinct_dashed_uuid4s():
    """The job id is telemetry's idempotency key, which the contract types as a uuid."""
    import uuid

    ids = {new_job_id() for _ in range(50)}
    assert len(ids) == 50
    for job_id in ids:
        assert uuid.UUID(job_id).version == 4
        assert job_id == str(uuid.UUID(job_id))


def test_a_new_transcript_never_overwrites_an_old_one(tmp_path):
    source = tmp_path / "meeting.m4a"
    assert output_path(source, tmp_path, "text", "j1") == tmp_path / "meeting.txt"
    (tmp_path / "meeting.txt").write_text("earlier")
    assert output_path(source, tmp_path, "text", "j1") == tmp_path / "meeting.j1.txt"


@pytest.mark.parametrize(
    ("fmt", "content", "expected"),
    [
        ("text", "  hello there  \n", "hello there"),
        ("json", '{"text": "hello there", "words": []}', "hello there"),
        ("json", "{broken", ""),
        (
            "srt",
            "1\n00:00:00,320 --> 00:00:03,840\nhello\nthere\n\n2\n00:00:04,000 --> "
            "00:00:05,000\nagain\n",
            "hello there again",
        ),
        ("vtt", "WEBVTT\n\n00:00.320 --> 00:03.840\nhello there\n", "hello there"),
        ("text", "Speaker 1: hello there\n\nSpeaker 2: again\n", "hello there\n\nagain"),
        (
            "srt",
            "1\n00:00:00,320 --> 00:00:03,840\nSpeaker 1: hello\nthere\n\n2\n"
            "00:00:04,000 --> 00:00:05,000\nSpeaker 2: again\n",
            "hello there again",
        ),
        (
            "vtt",
            "WEBVTT\n\n00:00.320 --> 00:03.840\n<v Speaker 1>hello\nthere\n\n"
            "00:04.000 --> 00:05.000\n<v Speaker 2>again</v>\n",
            "hello there again",
        ),
    ],
)
def test_the_words_come_out_of_every_format(tmp_path, fmt, content, expected):
    path = tmp_path / f"t.{fmt}"
    path.write_text(content)
    assert transcript_text(path, fmt) == expected


def test_records_live_under_jobs_and_carry_their_kind(tmp_path):
    store = JobStore(tmp_path)
    record = job()
    store.write(record)
    assert store.record_path(record.job_id).parent == tmp_path / "jobs"
    assert json.loads(store.record_path(record.job_id).read_text())["kind"] == TRANSCRIPTION
    assert isinstance(store.read(record.job_id), TranscriptionJob)


def test_a_record_of_an_unknown_kind_reads_as_none(tmp_path):
    store = JobStore(tmp_path)
    record = job()
    store.write(record)
    data = json.loads(store.record_path(record.job_id).read_text())
    store.record_path(record.job_id).write_text(json.dumps({**data, "kind": "karaoke"}))
    assert store.read(record.job_id) is None


def test_discarding_artifacts_leaves_the_record_and_log(tmp_path):
    store = JobStore(tmp_path)
    record = job()
    store.write(record)
    store.wav_path(record.job_id).write_bytes(b"RIFF")
    store.log_path(record.job_id).write_text("log")
    store.discard_artifacts(record.job_id)
    assert not store.wav_path(record.job_id).exists()
    assert store.read(record.job_id) is not None and store.log_path(record.job_id).exists()


def synthesis_at(tmp_path, **overrides: object):
    from voicebox.jobs.store import SynthesisJob

    fields = dict(
        job_id=new_job_id(),
        state=RUNNING,
        source="",
        output=str(tmp_path / "out.wav"),
        format="wav",
        nemo_binary="nemo-speech",
        claimed_by=1,
        created_at=1000.0,
        voice="John",
        tts_model="magpie",
        input_kind="text",
        input_format="plain",
        chars=3000,
        words=500,
        paragraphs=4,
        cold_start=False,
    )
    return SynthesisJob(**{**fields, **overrides})


def test_a_synthesis_job_round_trips_as_its_own_kind(tmp_path):
    from voicebox.jobs.store import SYNTHESIS, SynthesisJob

    store = JobStore(tmp_path)
    job = synthesis_at(tmp_path, chunks=6, chunks_done=2)
    store.write(job)
    read = store.read(job.job_id)
    assert isinstance(read, SynthesisJob) and read.kind == SYNTHESIS and read == job


def test_a_synthesis_estimate_counts_characters_and_a_cold_start(tmp_path):
    from voicebox.synthesize.limits import COLD_START_S, SYNTH_CHARS_PER_S

    warm = synthesis_at(tmp_path)
    cold = synthesis_at(tmp_path, cold_start=True)
    assert warm.eta_s == pytest.approx(3000 / SYNTH_CHARS_PER_S)
    assert cold.eta_s == pytest.approx(warm.eta_s + COLD_START_S)


def test_a_synthesis_jobs_staged_text_and_chunks_are_artifacts(tmp_path):
    store = JobStore(tmp_path)
    job = synthesis_at(tmp_path)
    store.write(job)
    store.text_path(job.job_id).write_text("hello")
    store.work_dir(job.job_id).mkdir()
    (store.work_dir(job.job_id) / "000000.wav").write_bytes(b"RIFF")
    store.discard_artifacts(job.job_id)
    assert not store.text_path(job.job_id).exists()
    assert not store.work_dir(job.job_id).exists()
    assert store.read(job.job_id) is not None


def test_a_synthesis_estimate_follows_the_pace_it_has_shown(tmp_path):
    from voicebox.synthesize.limits import COLD_START_S

    # 500 characters spoken in 50 s of synthesis: 10 a second, overall
    shown = {"chars_done": 500, "synth_ms": 50_000, "slowest_chars_per_s": 2.0}
    assert synthesis_at(tmp_path, **shown).eta_s == pytest.approx(300)
    cold = synthesis_at(tmp_path, cold_start=True, **shown)
    assert cold.eta_s == pytest.approx(300 + COLD_START_S)


def test_a_synthesis_partial_output_on_the_record_is_an_artifact(tmp_path):
    store = JobStore(tmp_path)
    partial = tmp_path / ".out.wav.x1y2.partial"
    partial.write_bytes(b"half")
    job = synthesis_at(tmp_path, partial=str(partial))
    store.write(job)
    store.discard_artifacts(job.job_id)
    assert not partial.exists()
    assert store.read(job.job_id).partial is None


def test_the_estimate_ignores_the_pace_of_requests_too_short_to_measure_it(tmp_path):
    from voicebox.synthesize.limits import PACE_MIN_CHARS, SYNTH_CHARS_PER_S

    # a heading's request: mostly fixed cost, so 6 characters in 0.8 s says nothing
    short = synthesis_at(tmp_path, chars=3000, chars_done=PACE_MIN_CHARS - 1, synth_ms=800)
    assert short.chars_per_s == SYNTH_CHARS_PER_S
    enough = synthesis_at(tmp_path, chars=3000, chars_done=PACE_MIN_CHARS, synth_ms=20_000)
    assert enough.chars_per_s == pytest.approx(PACE_MIN_CHARS / 20)


def test_a_malformed_partial_on_the_record_does_not_stop_the_rest_being_discarded(tmp_path):
    store = JobStore(tmp_path)
    job = synthesis_at(tmp_path)
    store.write(job)
    path = store.record_path(job.job_id)
    data = json.loads(path.read_text())
    data["partial"] = 12345
    path.write_text(json.dumps(data))
    store.work_dir(job.job_id).mkdir()
    (store.work_dir(job.job_id) / "000000.wav").write_bytes(b"RIFF")
    store.discard_artifacts(job.job_id)
    assert not store.work_dir(job.job_id).exists()
