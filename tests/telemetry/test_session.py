"""One session's write path into the local store. Nothing here may raise: a dead
telemetry backend has to be indistinguishable from a healthy one."""

import sqlite3
from pathlib import Path

from speak_core.telemetry import store

from voicebox.telemetry.contract import JOBS, SCHEMA
from voicebox.telemetry.record import insert, insert_at
from voicebox.telemetry.rows import TRANSCRIPTION, JobFacts, iso, job_row
from voicebox.telemetry.session import TelemetrySession

INSTALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def opened(path: Path) -> sqlite3.Connection:
    conn = store.connect(path, SCHEMA)
    store.upsert_install(
        conn,
        {
            "install_id": INSTALL,
            "first_seen_at": iso(1_790_000_000),
            "last_seen_at": iso(1_790_000_000),
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
            "started_at": iso(1_790_000_000),
            "plugin_version": "1.0.0",
            "core_version": "0.3.0",
            "harness": "claude-code",
            "ffmpeg_present": True,
        },
    )
    return conn


def row(status: str = "ok", job_id: str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc") -> dict:
    return job_row(
        JobFacts(
            job_id=job_id,
            session_id=SESSION,
            install_id=INSTALL,
            kind=TRANSCRIPTION,
            started_at=iso(1_790_000_000),
            ended_at=iso(1_790_000_010),
            wall_ms=10_000,
            status=status,
            engine="nemo",
            stage="asr",
        )
    )


def session(tmp_path: Path) -> TelemetrySession:
    path = tmp_path / "telemetry.db"
    return TelemetrySession(conn=opened(path), install_id=INSTALL, session_id=SESSION, db_path=path)


def test_a_recorded_row_is_in_the_backlog(tmp_path):
    s = session(tmp_path)
    s.record(row())
    assert [r["status"] for r in store.unshipped_rows(s.conn, JOBS)] == ["ok"]


def test_the_first_record_of_a_job_wins(tmp_path):
    """A crashed job can be found by two sessions; the job id keeps one row."""
    s = session(tmp_path)
    s.record(row("crashed"))
    s.record(row("ok"))
    assert [r["status"] for r in store.unshipped_rows(s.conn, JOBS)] == ["crashed"]


def test_insert_at_opens_the_store_by_path_the_way_a_worker_does(tmp_path):
    s = session(tmp_path)
    insert_at(s.db_path, row())
    assert len(store.unshipped_rows(s.conn, JOBS)) == 1


def test_an_unopenable_store_does_not_raise(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    insert_at(blocker / "telemetry.db", row())


def test_an_orphaned_row_is_dropped_quietly(tmp_path):
    s = session(tmp_path)
    orphan = {**row(), "session_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}
    insert(s.conn, orphan)
    assert store.unshipped_rows(s.conn, JOBS) == []


def test_the_ref_is_what_a_worker_needs_to_record_for_this_session(tmp_path):
    s = session(tmp_path)
    assert s.ref() == {"db_path": str(s.db_path), "session_id": SESSION, "install_id": INSTALL}


def test_close_writes_ended_at_and_puts_the_session_back_in_the_backlog(tmp_path):
    s = session(tmp_path)
    store.mark_sessions_shipped(s.conn, store.unshipped_sessions(s.conn, INSTALL), iso(1))
    s.close()
    (again,) = store.unshipped_sessions(s.conn, INSTALL)
    assert again["ended_at"] is not None


def test_a_declared_harness_version_is_cut_to_the_contract_length(tmp_path):
    s = session(tmp_path)
    s.update_harness("other", "v" * 100)
    (stored,) = store.sessions_by_id(s.conn, [SESSION])
    assert (stored["harness"], len(stored["harness_version"])) == ("other", 64)


def test_a_closed_connection_never_raises(tmp_path):
    s = session(tmp_path)
    s.conn.close()
    s.record(row())
    s.close()
    s.update_harness("other", None)
