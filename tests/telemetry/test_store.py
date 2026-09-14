"""Local SQLite: schema creation, FK enforcement, and the CRUD `ship.py` relies on."""

import sqlite3
from pathlib import Path

import pytest

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


def test_the_runtime_schema_matches_the_vendored_contract_copy() -> None:
    """`store.SCHEMA_SQL` is a bundled resource, not the vendored test copy - a
    second test proves they still say the same thing (tests/contract/test_freshness
    covers the vendored copy against the upstream repo)."""
    vendored = (Path(__file__).parent.parent / "contract" / "v1" / "schema.sql").read_text()
    assert vendored == store.SCHEMA_SQL


def test_connect_creates_every_table() -> None:
    conn = store.connect(Path(":memory:"))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"installs", "sessions", "utterances"} <= tables


def test_connect_is_idempotent_against_an_existing_file(tmp_path):
    path = tmp_path / "telemetry.db"
    store.connect(path).close()
    conn = store.connect(path)  # must not raise on the tables already being there
    conn.execute("SELECT 1 FROM installs")


def test_foreign_keys_are_enforced_on_every_connection() -> None:
    """spec 4.8: D1 enforces FKs unconditionally; sqlite3 defaults them off. An
    orphaned row must be refused locally the same way it is refused in production."""
    conn = store.connect(Path(":memory:"))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_utterance(conn, utterance(session_id="no-such-session"))


def test_a_session_needs_an_existing_install() -> None:
    conn = store.connect(Path(":memory:"))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_session(conn, {**SESSION, "install_id": "no-such-install"})


def test_upsert_install_refreshes_last_seen_at_without_a_second_row() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.upsert_install(conn, {**INSTALL, "last_seen_at": "2026-09-14T10:00:00Z"})

    (count,) = conn.execute("SELECT COUNT(*) FROM installs").fetchone()
    assert count == 1
    row = store.install_row(conn, "install-1")
    assert row is not None
    assert row["last_seen_at"] == "2026-09-14T10:00:00Z"


def test_install_row_with_no_id_returns_the_only_install() -> None:
    conn = store.connect(Path(":memory:"))
    assert store.install_row(conn) is None
    store.upsert_install(conn, INSTALL)
    row = store.install_row(conn)
    assert row is not None
    assert row["install_id"] == "install-1"


def test_insert_utterance_then_unshipped_utterances_round_trips() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    store.insert_utterance(conn, utterance())

    rows = store.unshipped_utterances(conn)
    assert len(rows) == 1
    assert rows[0]["utterance_id"] == "utterance-1"
    assert rows[0]["text"] == "hi"  # the local row keeps content columns


def test_mark_shipped_removes_a_row_from_the_backlog() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    store.insert_utterance(conn, utterance())

    store.mark_shipped(conn, ["utterance-1"], "2026-09-13T10:05:00Z")

    assert store.unshipped_utterances(conn) == []


def test_end_session_sets_ended_at() -> None:
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)

    store.end_session(conn, "session-1", "2026-09-13T10:10:00Z")

    sessions = store.sessions_by_id(conn, ["session-1"])
    assert sessions[0]["ended_at"] == "2026-09-13T10:10:00Z"


def test_sessions_by_id_returns_nothing_for_an_empty_request() -> None:
    conn = store.connect(Path(":memory:"))
    assert store.sessions_by_id(conn, []) == []


def test_the_unshipped_backlog_is_capped_oldest_first() -> None:
    """spec 11: the unshipped backlog is capped at 10,000 rows, oldest dropped, so
    an unreachable server cannot grow the local file without limit."""
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)

    for n in range(5):
        store.insert_utterance(
            conn,
            utterance(
                utterance_id=f"utterance-{n}",
                occurred_at=f"2026-09-13T10:00:0{n}Z",
            ),
            backlog_cap=3,
        )

    rows = store.unshipped_utterances(conn)
    assert [row["utterance_id"] for row in rows] == ["utterance-2", "utterance-3", "utterance-4"]
