"""A transcription is recorded by a worker process, not by the session object."""

from pathlib import Path

from speak.queue import UtteranceOutcome
from speak.telemetry import store
from speak.telemetry.contract import SCHEMA
from speak.telemetry.session import TelemetrySession, TranscriptionOutcome, record_transcription

INSTALL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def opened(tmp_path: Path):
    conn = store.connect(tmp_path / "telemetry.db", SCHEMA)
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
        SCHEMA,
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
    (row,) = store.unshipped_rows(conn, SCHEMA.rows)
    assert (row["direction"], row["source"], row["status"]) == ("in", "transcribe", "ok")
    assert (row["source_bytes"], row["audio_codec"], row["format"]) == (1024, "pcm_s16le", "text")
    assert (row["converted"], row["diarize"]) == (0, 1)
    assert row["device_kind"] is None


# Every column a transcription row is allowed to carry a value in. Anything else
# in the `utterances` table - `text`, `audio_path`, `device_name`, or a column
# nobody has thought to add yet - must come back None. Spot-checking a couple of
# known-sensitive columns would pass on a regression that wrote a filename or a
# transcript into some other column entirely; this is the check that would not.
MAY_BE_SET = {
    "utterance_id",
    "session_id",
    "install_id",
    "seq",
    "direction",
    "source",
    "occurred_at",
    "engine",
    "model",
    "words",
    "chars",
    "audio_ms",
    "asr_ms",
    "status",
    "source_bytes",
    "audio_codec",
    "converted",
    "diarize",
    "format",
}


def test_a_transcription_carries_nothing_outside_its_own_columns(tmp_path):
    conn = opened(tmp_path)
    record_transcription(
        conn,
        session_id=SESSION_ID,
        install_id=INSTALL_ID,
        engine="nemo",
        model="nemotron-3.5",
        outcome=outcome(),
    )
    (row,) = store.unshipped_rows(conn, SCHEMA.rows)
    unexpectedly_set = {
        column: value
        for column, value in row.items()
        if column not in MAY_BE_SET and value is not None
    }
    assert unexpectedly_set == {}


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
    assert [r["seq"] for r in store.unshipped_rows(conn, SCHEMA.rows)] == [1, 2, 3]


def test_seq_stays_unique_when_the_session_and_a_worker_interleave(tmp_path):
    """The server's own rows and a transcription worker's rows land in the same
    session from two connections. A counter held by either side hands out a number
    the other has already used; `seq` assigned inside the INSERT does not."""
    session_conn = opened(tmp_path)
    worker_conn = store.connect(tmp_path / "telemetry.db", SCHEMA)
    session = TelemetrySession(
        conn=session_conn,
        install_id=INSTALL_ID,
        session_id=SESSION_ID,
        tts_engine="say",
        tts_model=None,
        asr_engine="nemo",
        asr_model="nemotron-3.5",
    )
    spoken = UtteranceOutcome(
        text="hello",
        voice=None,
        intent=None,
        status="ok",
        queue_wait_ms=0,
        synth_ms=None,
        play_ms=10,
        audio_ms=None,
    )
    for _ in range(3):
        session.utterance_finished(spoken)
        record_transcription(
            worker_conn,
            session_id=SESSION_ID,
            install_id=INSTALL_ID,
            engine="nemo",
            model=None,
            outcome=outcome(),
        )
        session.heard(source="listen", status="heard_nothing")

    rows = store.unshipped_rows(session_conn, SCHEMA.rows)
    assert sorted(r["seq"] for r in rows) == list(range(1, 10))
    assert [r["source"] for r in sorted(rows, key=lambda r: r["seq"])][:3] == [
        "speak",
        "transcribe",
        "listen",
    ]


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
