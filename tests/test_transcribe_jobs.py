import time
from dataclasses import replace
from pathlib import Path

import pytest

from speak.transcribe.jobs import DONE, RETENTION_S, RUNNING, Job, JobStore, eta_seconds, new_job_id
from speak.transcribe.output import output_path, transcript_text


def job(**overrides: object) -> Job:
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
    return Job(**{**fields, **overrides})


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


def test_the_log_tail_is_the_last_lines(tmp_path):
    store = JobStore(tmp_path)
    store.log_path("abc123").parent.mkdir(parents=True, exist_ok=True)
    store.log_path("abc123").write_text("\n".join(f"line {i}" for i in range(20)))
    assert store.log_tail("abc123", lines=2) == "line 18\nline 19"


def test_the_estimate_uses_the_measured_ratios():
    assert eta_seconds(690.0, diarize=True) == pytest.approx(100.0)
    assert eta_seconds(950.0, diarize=False) == pytest.approx(100.0)


def test_job_ids_are_short_and_distinct():
    ids = {new_job_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 12 for i in ids)


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
    ],
)
def test_the_words_come_out_of_every_format(tmp_path, fmt, content, expected):
    path = tmp_path / f"t.{fmt}"
    path.write_text(content)
    assert transcript_text(path, fmt) == expected
