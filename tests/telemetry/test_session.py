"""`TelemetrySession`: turns queue outcomes and heard attempts into stored rows,
never blocking and never raising out to its caller."""

from pathlib import Path

from speak.queue import UtteranceOutcome
from speak.telemetry import store
from speak.telemetry.session import TelemetrySession, classify_device, wav_duration_ms

INSTALL = {
    "install_id": "install-1",
    "first_seen_at": "2026-09-13T10:00:00Z",
    "last_seen_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "os": "darwin",
    "arch": "arm64",
    "accel": "metal",
}

SESSION_ROW = {
    "session_id": "session-1",
    "install_id": "install-1",
    "started_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "listen_enabled": True,
    "harness": "claude-code",
}


def build_session(**overrides: object) -> TelemetrySession:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION_ROW)
    kwargs: dict[str, object] = {
        "conn": conn,
        "install_id": "install-1",
        "session_id": "session-1",
        "tts_engine": "nemo",
        "tts_model": "magpie",
        "asr_engine": "nemo",
        "asr_model": "nemotron-3.5",
    }
    kwargs.update(overrides)
    return TelemetrySession(**kwargs)


def one_row(session: TelemetrySession) -> dict[str, object]:
    rows = store.unshipped_utterances(session.conn)
    assert len(rows) == 1
    return rows[0]


def test_classify_device_recognises_common_hints() -> None:
    assert classify_device("Danilo's AirPods Pro") == "bluetooth"
    assert classify_device("USB Microphone") == "usb"
    assert classify_device("MacBook Pro Microphone") == "builtin"
    assert classify_device("BlackHole 2ch") == "virtual"
    assert classify_device("Weird Interface") == "unknown"
    assert classify_device(None) == "unknown"


def test_wav_duration_ms_reads_the_header() -> None:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)  # one second of silence
    assert wav_duration_ms(buffer.getvalue()) == 1000


def test_wav_duration_ms_is_none_for_garbage() -> None:
    assert wav_duration_ms(b"not a wav") is None


def test_utterance_finished_writes_one_out_row() -> None:
    session = build_session()
    outcome = UtteranceOutcome(
        text="hello there",
        voice="Sofia",
        intent="task_done",
        status="ok",
        queue_wait_ms=10,
        synth_ms=200,
        play_ms=1500,
        audio_ms=1400,
    )
    session.utterance_finished(outcome)

    row = one_row(session)
    assert row["direction"] == "out"
    assert row["source"] == "speak"
    assert row["engine"] == "nemo"
    assert row["model"] == "magpie"
    assert row["voice"] == "Sofia"
    assert row["intent"] == "task_done"
    assert row["words"] == 2
    assert row["chars"] == len("hello there")
    assert row["status"] == "ok"
    assert row["text"] == "hello there"
    assert row["text_sha256"]


def test_utterance_finished_defaults_intent_to_unspecified() -> None:
    session = build_session()
    outcome = UtteranceOutcome(
        text="hi",
        voice=None,
        intent=None,
        status="ok",
        queue_wait_ms=0,
        synth_ms=None,
        play_ms=None,
        audio_ms=None,
    )
    session.utterance_finished(outcome)
    assert one_row(session)["intent"] == "unspecified"


def test_utterance_finished_assigns_increasing_seq() -> None:
    session = build_session()
    outcome = UtteranceOutcome(
        text="one",
        voice=None,
        intent=None,
        status="ok",
        queue_wait_ms=0,
        synth_ms=None,
        play_ms=None,
        audio_ms=None,
    )
    session.utterance_finished(outcome)
    session.utterance_finished(outcome)
    rows = sorted(store.unshipped_utterances(session.conn), key=lambda r: r["seq"])
    assert [row["seq"] for row in rows] == [1, 2]


def test_a_broken_insert_does_not_raise_out_of_utterance_finished(monkeypatch) -> None:
    """Rule 2: a dead telemetry backend must be indistinguishable from a healthy one."""
    session = build_session()

    def explode(*args: object, **kwargs: object) -> None:
        import sqlite3

        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(store, "insert_utterance", explode)
    outcome = UtteranceOutcome(
        text="hi",
        voice=None,
        intent=None,
        status="ok",
        queue_wait_ms=0,
        synth_ms=None,
        play_ms=None,
        audio_ms=None,
    )
    session.utterance_finished(outcome)  # must not raise


def test_heard_writes_one_in_row_with_device_kind_and_no_device_name_upload_concern() -> None:
    session = build_session()
    session.heard(
        source="listen",
        status="ok",
        transcript="run the tests",
        device_name="Danilo's AirPods Pro",
        asr_ms=250,
        wav=None,
    )
    row = one_row(session)
    assert row["direction"] == "in"
    assert row["source"] == "listen"
    assert row["engine"] == "nemo"
    assert row["model"] == "nemotron-3.5"
    assert row["device_kind"] == "bluetooth"
    assert row["device_name"] == "Danilo's AirPods Pro"  # kept locally
    assert row["words"] == 3
    assert row["asr_ms"] == 250
    assert row["status"] == "ok"


def test_heard_with_no_transcript_leaves_words_and_chars_unset() -> None:
    session = build_session()
    session.heard(source="listen", status="refused")
    row = one_row(session)
    assert row["words"] is None
    assert row["chars"] is None
    assert row["text"] is None


def test_heard_never_raises_when_recording_is_declined(tmp_path) -> None:
    session = build_session(record_audio=True, recordings_dir=tmp_path / "does" / "not" / "exist")
    session.heard(source="listen", status="ok", transcript="hi", wav=b"not really a wav")
    # No exception, and the row is still written even though nothing could be saved.
    assert one_row(session)["status"] == "ok"


def test_heard_saves_the_recording_when_the_user_opted_in(tmp_path) -> None:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 100)
    wav_bytes = buffer.getvalue()

    session = build_session(record_audio=True, recordings_dir=tmp_path)
    session.heard(source="listen", status="ok", transcript="hi", wav=wav_bytes)

    row = one_row(session)
    assert row["audio_path"] is not None
    assert Path(row["audio_path"]).read_bytes() == wav_bytes
    assert row["audio_sha256"]


def test_recording_is_never_saved_without_opting_in(tmp_path) -> None:
    session = build_session(record_audio=False, recordings_dir=tmp_path)
    session.heard(source="listen", status="ok", transcript="hi", wav=b"RIFF....WAVEfmt ")
    row = one_row(session)
    assert row["audio_path"] is None
    assert list(tmp_path.iterdir()) == []


def test_close_sets_ended_at() -> None:
    session = build_session()
    session.close()
    rows = store.sessions_by_id(session.conn, ["session-1"])
    assert rows[0]["ended_at"] is not None
