"""A transcription is recorded by a worker process, not by the session object."""

from pathlib import Path

from speak.telemetry import store
from speak.telemetry.session import TranscriptionOutcome, record_transcription

INSTALL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def opened(tmp_path: Path):
    conn = store.connect(tmp_path / "telemetry.db")
    store.upsert_install(
        conn,
        {
            "install_id": INSTALL_ID,
            "first_seen_at": "2026-09-21T10:00:00Z",
            "last_seen_at": "2026-09-21T10:00:00Z",
            "plugin_version": "1.5.0",
            "os": "darwin",
            "arch": "arm64",
            "accel": "metal",
        },
    )
    store.insert_session(
        conn,
        {
            "session_id": SESSION_ID,
            "install_id": INSTALL_ID,
            "started_at": "2026-09-21T10:00:00Z",
            "plugin_version": "1.5.0",
            "listen_enabled": 1,
            "harness": "claude-code",
        },
    )
    return conn


def outcome(**overrides: object) -> TranscriptionOutcome:
    fields = dict(
        status="ok",
        source_bytes=1024,
        audio_codec="pcm_s16le",
        audio_ms=63_000,
        asr_ms=9_100,
        converted=False,
        diarize=True,
        format="text",
        words=216,
        chars=1343,
    )
    return TranscriptionOutcome(**{**fields, **overrides})


def test_a_transcription_is_an_in_row_from_the_transcribe_source(tmp_path):
    conn = opened(tmp_path)
    record_transcription(
        conn,
        session_id=SESSION_ID,
        install_id=INSTALL_ID,
        engine="nemo",
        model="nemotron-3.5",
        outcome=outcome(),
    )
    (row,) = store.unshipped_utterances(conn)
    assert (row["direction"], row["source"], row["status"]) == ("in", "transcribe", "ok")
    assert (row["source_bytes"], row["audio_codec"], row["format"]) == (1024, "pcm_s16le", "text")
    assert (row["converted"], row["diarize"]) == (0, 1)
    assert row["device_kind"] is None


def test_a_transcription_carries_no_text_and_no_path(tmp_path):
    conn = opened(tmp_path)
    record_transcription(
        conn,
        session_id=SESSION_ID,
        install_id=INSTALL_ID,
        engine="nemo",
        model="nemotron-3.5",
        outcome=outcome(),
    )
    (row,) = store.unshipped_utterances(conn)
    assert row["text"] is None
    assert row["audio_path"] is None


def test_the_seq_follows_whatever_the_session_already_wrote(tmp_path):
    conn = opened(tmp_path)
    for _ in range(3):
        record_transcription(
            conn,
            session_id=SESSION_ID,
            install_id=INSTALL_ID,
            engine="nemo",
            model=None,
            outcome=outcome(),
        )
    assert [r["seq"] for r in store.unshipped_utterances(conn)] == [1, 2, 3]
    assert store.next_seq(conn, SESSION_ID) == 4


def test_recording_never_raises(tmp_path):
    conn = opened(tmp_path)
    conn.close()
    # No assert: the behaviour under test is that this call does not raise even
    # though `conn` is closed - pytest already fails the test if it does.
    record_transcription(
        conn,
        session_id=SESSION_ID,
        install_id=INSTALL_ID,
        engine="nemo",
        model=None,
        outcome=outcome(),
    )
