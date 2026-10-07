"""voicebox-stats: this install's own jobs - kinds, statuses, speed and failures."""

from pathlib import Path

from speak_core.telemetry import store

from voicebox.telemetry.contract import JOBS, SCHEMA
from voicebox.telemetry.rows import NARRATION, SYNTHESIS, TRANSCRIPTION, JobFacts, iso, job_row
from voicebox.telemetry.stats import render

INSTALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def opened():
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(
        conn,
        {
            "install_id": INSTALL,
            "first_seen_at": iso(0),
            "last_seen_at": iso(0),
            "plugin_version": "1.0.0",
            "os": "darwin",
            "arch": "arm64",
            "accel": "metal",
        },
    )
    store.insert_session(
        conn,
        SCHEMA,
        {
            "session_id": SESSION,
            "install_id": INSTALL,
            "started_at": iso(0),
            "plugin_version": "1.0.0",
            "core_version": "0.3.0",
            "harness": "claude-code",
            "ffmpeg_present": True,
        },
    )
    return conn


def add(conn, n: int, kind: str, status: str, fields=None, **facts: object) -> None:
    row = job_row(
        JobFacts(
            job_id=f"cccccccc-cccc-4ccc-8ccc-{n:012d}",
            session_id=SESSION,
            install_id=INSTALL,
            kind=kind,
            started_at=iso(n),
            ended_at=iso(n + 1),
            wall_ms=1000,
            status=status,
            engine="nemo",
            **facts,
        ),
        fields,
    )
    store.insert_row(conn, JOBS, row)


def test_an_empty_store_says_so():
    assert render(opened()) == "No jobs recorded yet."


def test_totals_kinds_and_statuses():
    conn = opened()
    add(conn, 1, TRANSCRIPTION, "ok")
    add(conn, 2, TRANSCRIPTION, "ok")
    add(conn, 3, SYNTHESIS, "refused", reason="busy")
    text = render(conn)
    assert text.splitlines()[0] == "3 jobs across 1 sessions; 3 unshipped."
    assert "transcription  ok       2" in text
    assert "synthesis      refused  1" in text


def test_speed_is_model_time_over_audio_time_per_kind():
    conn = opened()
    add(conn, 1, TRANSCRIPTION, "ok", {"asr_ms": 1000, "audio_ms": 10_000})
    add(conn, 2, SYNTHESIS, "ok", {"synth_ms": 5000, "audio_ms": 10_000})
    add(conn, 3, NARRATION, "ok", {"synth_ms": 2000, "audio_ms": 4000})
    text = render(conn)
    assert "transcription  0.10x over 1 job" in text
    assert "synthesis      0.50x over 1 job" in text
    assert "narration      0.50x over 1 job" in text


def test_recent_failures_show_the_local_error_message():
    conn = opened()
    add(
        conn,
        1,
        SYNTHESIS,
        "failed",
        reason="timeout",
        stage="synthesize",
        error_message="chunk 3 of 9: no answer",
    )
    assert "synthesis      timeout at synthesize: chunk 3 of 9: no answer" in render(conn)


def test_rejected_rows_are_counted_apart_from_the_backlog():
    conn = opened()
    add(conn, 1, TRANSCRIPTION, "ok")
    store.mark_rejected(conn, JOBS, ["cccccccc-cccc-4ccc-8ccc-000000000001"], iso(5))
    assert render(conn).splitlines()[0].endswith("0 unshipped, 1 rejected by the server.")
