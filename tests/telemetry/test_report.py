"""`speak-stats`: plain SQL over the raw tables, rendered as text."""

from pathlib import Path

from speak.telemetry import report, store

INSTALL = {
    "install_id": "install-1",
    "first_seen_at": "2026-09-13T10:00:00Z",
    "last_seen_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "os": "darwin",
    "arch": "arm64",
    "accel": "metal",
}

SESSION = {
    "session_id": "session-1",
    "install_id": "install-1",
    "started_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "listen_enabled": False,
    "harness": "claude-code",
}


def utterance(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "utterance_id": "utterance-1",
        "session_id": "session-1",
        "install_id": "install-1",
        "seq": 1,
        "direction": "out",
        "source": "speak",
        "occurred_at": "2026-09-13T10:00:01Z",
        "status": "ok",
        "intent": "task_done",
        "engine": "nemo",
        "synth_ms": 200,
    }
    row.update(overrides)
    return row


def test_render_on_an_empty_store_says_so() -> None:
    conn = store.connect(Path(":memory:"))
    assert report.render(conn) == "No utterances recorded yet."


def test_render_reports_totals_and_the_interruption_rate() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    store.insert_utterance(conn, utterance(utterance_id="u1", status="ok"))
    store.insert_utterance(
        conn, utterance(utterance_id="u2", seq=2, status="discarded", intent="progress")
    )

    text = report.render(conn)
    assert "2 utterances" in text
    assert "progress" in text
    assert "1/1 (100%)" in text  # progress: 1 discarded of 1


def test_render_reports_voice_input_when_there_is_any() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    store.insert_utterance(
        conn,
        utterance(
            utterance_id="u1", direction="in", source="listen", status="heard_nothing", intent=None
        ),
    )
    text = report.render(conn)
    assert "Voice input: 1 recordings, 1 heard nothing." in text


def test_main_with_no_store_prints_a_plain_message(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    report.main()
    assert "No telemetry recorded yet." in capsys.readouterr().out
