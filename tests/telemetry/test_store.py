"""Local SQLite: schema creation, FK enforcement, and the CRUD `ship.py` relies on."""

import sqlite3
from pathlib import Path

import pytest
from telemetry_fixtures import UTTERANCES

from speak.telemetry import store

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
    "tts_engine": "say",
    "asr_engine": None,
    "listen_enabled": False,
    "harness": "claude-code",
}


def utterance(**overrides: object) -> dict[str, object]:
    row = {
        "utterance_id": "utterance-1",
        "session_id": "session-1",
        "install_id": "install-1",
        "seq": 1,
        "direction": "out",
        "source": "speak",
        "occurred_at": "2026-09-13T10:00:01Z",
        "status": "ok",
        "text": "hi",
        "text_sha256": "abc",
    }
    row.update(overrides)
    return row


def test_connect_creates_every_table() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"installs", "sessions", "utterances"} <= tables


def test_connect_is_idempotent_against_an_existing_file(tmp_path):
    path = tmp_path / "telemetry.db"
    store.connect(path, UTTERANCES).close()
    conn = store.connect(path, UTTERANCES)  # must not raise on the tables already being there
    conn.execute("SELECT 1 FROM installs")


def test_foreign_keys_are_enforced_on_every_connection() -> None:
    """spec 4.8: D1 enforces FKs unconditionally; sqlite3 defaults them off. An
    orphaned row must be refused locally the same way it is refused in production."""
    conn = store.connect(Path(":memory:"), UTTERANCES)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_row(conn, UTTERANCES.rows, utterance(session_id="no-such-session"))


def test_a_session_needs_an_existing_install() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_session(conn, UTTERANCES, {**SESSION, "install_id": "no-such-install"})


def test_upsert_install_refreshes_last_seen_at_without_a_second_row() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.upsert_install(conn, {**INSTALL, "last_seen_at": "2026-09-14T10:00:00Z"})

    (count,) = conn.execute("SELECT COUNT(*) FROM installs").fetchone()
    assert count == 1
    row = store.install_row(conn, "install-1")
    assert row is not None
    assert row["last_seen_at"] == "2026-09-14T10:00:00Z"


def test_install_row_with_no_id_returns_the_only_install() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    assert store.install_row(conn) is None
    store.upsert_install(conn, INSTALL)
    row = store.install_row(conn)
    assert row is not None
    assert row["install_id"] == "install-1"


def test_insert_row_then_unshipped_rows_round_trips() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)
    store.insert_row(conn, UTTERANCES.rows, utterance())

    rows = store.unshipped_rows(conn, UTTERANCES.rows)
    assert len(rows) == 1
    assert rows[0]["utterance_id"] == "utterance-1"
    assert rows[0]["text"] == "hi"  # the local row keeps content columns


def test_mark_shipped_removes_a_row_from_the_backlog() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)
    store.insert_row(conn, UTTERANCES.rows, utterance())

    store.mark_shipped(conn, UTTERANCES.rows, ["utterance-1"], "2026-09-13T10:05:00Z")

    assert store.unshipped_rows(conn, UTTERANCES.rows) == []


def test_end_session_sets_ended_at() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)

    store.end_session(conn, "session-1", "2026-09-13T10:10:00Z")

    sessions = store.sessions_by_id(conn, ["session-1"])
    assert sessions[0]["ended_at"] == "2026-09-13T10:10:00Z"


def test_sessions_by_id_returns_nothing_for_an_empty_request() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    assert store.sessions_by_id(conn, []) == []


def test_the_unshipped_backlog_is_capped_oldest_first() -> None:
    """spec 11: the unshipped backlog is capped at 10,000 rows, oldest dropped, so
    an unreachable server cannot grow the local file without limit."""
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)

    for n in range(5):
        store.insert_row(
            conn,
            UTTERANCES.rows,
            utterance(
                utterance_id=f"utterance-{n}",
                occurred_at=f"2026-09-13T10:00:0{n}Z",
            ),
            backlog_cap=3,
        )

    rows = store.unshipped_rows(conn, UTTERANCES.rows)
    assert [row["utterance_id"] for row in rows] == ["utterance-2", "utterance-3", "utterance-4"]


def test_updating_a_session_puts_it_back_in_the_backlog() -> None:
    """Every write to a session row clears `shipped_at`, or the server keeps the
    version it was first sent - a session with no end and the wrong harness."""
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)

    def ship_all() -> None:
        store.mark_sessions_shipped(conn, store.unshipped_sessions(conn, "install-1"), "t")
        assert store.unshipped_sessions(conn, "install-1") == []

    ship_all()
    store.end_session(conn, "session-1", "2026-09-13T11:00:00Z")
    assert len(store.unshipped_sessions(conn, "install-1")) == 1

    ship_all()
    store.update_session_harness(conn, "session-1", "codex", "1.0")
    assert len(store.unshipped_sessions(conn, "install-1")) == 1


def test_a_rejected_row_leaves_the_backlog_and_the_cap() -> None:
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)
    store.insert_row(conn, UTTERANCES.rows, utterance(utterance_id="bad"))
    store.mark_rejected(conn, UTTERANCES.rows, ["bad"], "t")
    store.insert_row(conn, UTTERANCES.rows, utterance(utterance_id="good"), backlog_cap=1)

    assert [r["utterance_id"] for r in store.unshipped_rows(conn, UTTERANCES.rows)] == ["good"]
    (count,) = conn.execute("SELECT COUNT(*) FROM utterances").fetchone()
    assert count == 2  # the cap counted only the backlog, so nothing was pruned
