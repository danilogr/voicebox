"""Local SQLite: the full schema from `contract/v1/schema.sql`, and plain CRUD over it.

The local database is the outbound queue, so there is one mechanism rather than a
database plus a spool file. An utterance is in the backlog until it is shipped or the
server refuses it (`shipped_at` and `rejected_at` both NULL); a session is until it
has been sent as it stands now (`shipped_at` NULL, cleared by every update to it).

D1 enforces foreign keys on every query, unconditionally; Python's `sqlite3` defaults
`PRAGMA foreign_keys` to off, per connection. Every connection this module opens turns
it on, so an orphaned row is refused locally the same way it would be in production -
without this, local tests pass on rows production rejects (spec 4.8).
"""

import importlib.resources
import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_SQL = importlib.resources.files("speak.telemetry").joinpath("schema.sql").read_text()

# Rows with more unshipped utterances than this are pruned, oldest first, so a
# server that becomes permanently unreachable cannot grow the local file forever.
DEFAULT_BACKLOG_CAP = 10_000

_UTTERANCE_COLUMNS = (
    "utterance_id",
    "session_id",
    "install_id",
    "seq",
    "direction",
    "source",
    "occurred_at",
    "engine",
    "model",
    "voice",
    "device_kind",
    "intent",
    "words",
    "chars",
    "audio_ms",
    "queue_wait_ms",
    "synth_ms",
    "play_ms",
    "asr_ms",
    "status",
    "source_bytes",
    "audio_codec",
    "converted",
    "diarize",
    "format",
    "text",
    "text_sha256",
    "audio_path",
    "audio_sha256",
    "device_name",
    "corrected_text",
    "corrected_at",
    "correction_kind",
    "shipped_at",
    "rejected_at",
)

# The backlog: neither shipped nor refused by the server. One definition, shared by
# the read, the cap and `speak-stats`.
UTTERANCE_BACKLOG = "shipped_at IS NULL AND rejected_at IS NULL"

_INSTALL_COLUMNS = (
    "install_id",
    "first_seen_at",
    "last_seen_at",
    "plugin_version",
    "disclosure_ver",
    "country",
    "language",
    "timezone",
    "utc_offset_min",
    "os",
    "os_version",
    "arch",
    "cpu_model",
    "cpu_count",
    "ram_gb",
    "accel",
    "accel_name",
)

_SESSION_COLUMNS = (
    "session_id",
    "install_id",
    "started_at",
    "ended_at",
    "plugin_version",
    "tts_engine",
    "asr_engine",
    "listen_enabled",
    "harness",
    "harness_version",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _idempotent(sql: str) -> str:
    """`schema.sql` runs once against a fresh D1 database and has no `IF NOT EXISTS`.

    The local file persists across every session, so creating its tables has to be
    idempotent without changing what they end up looking like.
    """
    sql = re.sub(r"CREATE TABLE (\w)", r"CREATE TABLE IF NOT EXISTS \1", sql)
    return re.sub(r"CREATE INDEX (\w)", r"CREATE INDEX IF NOT EXISTS \1", sql)


def connect(path: Path) -> sqlite3.Connection:
    """Open the local store, creating its schema and directory if needed.

    `PRAGMA foreign_keys = ON` is set here and nowhere else needs to remember to:
    every connection this module hands out is safe by construction.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_idempotent(SCHEMA_SQL))
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _upsert(
    conn: sqlite3.Connection, table: str, columns: Sequence[str], row: dict[str, Any]
) -> None:
    present = [c for c in columns if c in row]
    placeholders = ", ".join(f":{c}" for c in present)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in present if c != columns[0])
    sql = (
        f"INSERT INTO {table} ({', '.join(present)}) VALUES ({placeholders}) "
        f"ON CONFLICT({columns[0]}) DO UPDATE SET {assignments}"
    )
    conn.execute(sql, row)
    conn.commit()


def upsert_install(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or refresh the one row for this install. Called on every session start,
    per CONTRACT.md: `last_seen_at` and the machine snapshot are meant to move."""
    _upsert(conn, "installs", _INSTALL_COLUMNS, row)


def install_row(conn: sqlite3.Connection, install_id: str | None = None) -> dict[str, Any] | None:
    if install_id is None:
        cursor = conn.execute("SELECT * FROM installs LIMIT 1")
    else:
        cursor = conn.execute("SELECT * FROM installs WHERE install_id = ?", (install_id,))
    row = cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


def insert_session(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Record one session. `session_id` is generated fresh per process, so a plain
    insert is enough - there is nothing to conflict with."""
    present = [c for c in _SESSION_COLUMNS if c in row]
    placeholders = ", ".join(f":{c}" for c in present)
    conn.execute(f"INSERT INTO sessions ({', '.join(present)}) VALUES ({placeholders})", row)
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str, ended_at: str) -> None:
    """Close the session row. Clearing `shipped_at` puts it back in the backlog, which
    is how `ended_at` reaches the server: the session already shipped when it began."""
    conn.execute(
        "UPDATE sessions SET ended_at = ?, shipped_at = NULL WHERE session_id = ?",
        (ended_at, session_id),
    )
    conn.commit()


def update_session_harness(
    conn: sqlite3.Connection, session_id: str, harness: str, harness_version: str | None
) -> None:
    """Correct the harness recorded at session start once the MCP `client_info` a
    request carries is known - the better source (spec 2), available only once a
    request is in flight, never at the moment the session row is first written."""
    conn.execute(
        "UPDATE sessions SET harness = ?, harness_version = ?, shipped_at = NULL "
        "WHERE session_id = ?",
        (harness, harness_version, session_id),
    )
    conn.commit()


def unshipped_sessions(conn: sqlite3.Connection, install_id: str) -> list[dict[str, Any]]:
    """This install's sessions that changed since they were last sent, or never were."""
    cursor = conn.execute(
        "SELECT * FROM sessions WHERE shipped_at IS NULL AND install_id = ? "
        "ORDER BY started_at ASC, rowid ASC",
        (install_id,),
    )
    return [_row_to_dict(row) for row in cursor.fetchall()]


def mark_sessions_shipped(
    conn: sqlite3.Connection, sessions: Sequence[dict[str, Any]], shipped_at: str
) -> None:
    """Mark each session shipped, unless it changed after it was read.

    The columns an update can touch are compared with what was sent, so a session
    closed or corrected while its request was in flight stays in the backlog with
    its new values instead of being marked as though the server had them.
    """
    conn.executemany(
        "UPDATE sessions SET shipped_at = ? WHERE session_id = ? "
        "AND ended_at IS ? AND harness IS ? AND harness_version IS ?",
        [
            (shipped_at, s["session_id"], s["ended_at"], s["harness"], s["harness_version"])
            for s in sessions
        ],
    )
    conn.commit()


def sessions_by_id(conn: sqlite3.Connection, session_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = list(session_ids)
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    cursor = conn.execute(f"SELECT * FROM sessions WHERE session_id IN ({placeholders})", ids)
    return [_row_to_dict(row) for row in cursor.fetchall()]


def insert_utterance(
    conn: sqlite3.Connection, row: dict[str, Any], *, backlog_cap: int = DEFAULT_BACKLOG_CAP
) -> None:
    """Insert one utterance row and enforce the unshipped-backlog cap.

    `seq` is assigned here and nowhere else, inside the INSERT itself, as one more
    than the session's highest so far. A transcription worker is its own process
    writing to the same session as the server, so a counter held by either would
    collide with the other's rows; one statement under SQLite's single write lock
    cannot. Any `seq` the caller passes is ignored.

    A missing `session_id` (or one that names no session) is refused by the foreign
    key, exactly as D1 refuses it in production.
    """
    present = [c for c in _UTTERANCE_COLUMNS if c in row and c != "seq"]
    placeholders = ", ".join(f":{c}" for c in present)
    conn.execute(
        f"INSERT INTO utterances ({', '.join(present)}, seq) VALUES ({placeholders}, "
        "(SELECT COALESCE(MAX(seq), 0) + 1 FROM utterances WHERE session_id = :session_id))",
        row,
    )
    conn.commit()
    _enforce_backlog_cap(conn, backlog_cap)


def _enforce_backlog_cap(conn: sqlite3.Connection, cap: int) -> None:
    (count,) = conn.execute(f"SELECT COUNT(*) FROM utterances WHERE {UTTERANCE_BACKLOG}").fetchone()
    over = count - cap
    if over <= 0:
        return
    conn.execute(
        f"""
        DELETE FROM utterances WHERE utterance_id IN (
            SELECT utterance_id FROM utterances
            WHERE {UTTERANCE_BACKLOG}
            ORDER BY occurred_at ASC, rowid ASC
            LIMIT ?
        )
        """,
        (over,),
    )
    conn.commit()
    logger.debug("telemetry backlog over %d rows; dropped the oldest %d", cap, over)


def unshipped_utterances(
    conn: sqlite3.Connection, install_id: str | None = None
) -> list[dict[str, Any]]:
    """The backlog, oldest first - one install's, or every install's when `None`.

    Bounded by construction: `insert_utterance` never lets the backlog grow past its
    cap, so this is always a small, in-memory-sized read.
    """
    where = UTTERANCE_BACKLOG + ("" if install_id is None else " AND install_id = ?")
    cursor = conn.execute(
        f"SELECT * FROM utterances WHERE {where} ORDER BY occurred_at ASC, rowid ASC",
        () if install_id is None else (install_id,),
    )
    return [_row_to_dict(row) for row in cursor.fetchall()]


def _stamp(conn: sqlite3.Connection, column: str, utterance_ids: Sequence[str], at: str) -> None:
    if not utterance_ids:
        return
    conn.executemany(
        f"UPDATE utterances SET {column} = ? WHERE utterance_id = ?",
        [(at, uid) for uid in utterance_ids],
    )
    conn.commit()


def mark_shipped(conn: sqlite3.Connection, utterance_ids: Sequence[str], shipped_at: str) -> None:
    _stamp(conn, "shipped_at", utterance_ids, shipped_at)


def mark_rejected(conn: sqlite3.Connection, utterance_ids: Sequence[str], rejected_at: str) -> None:
    """Take rows the server refused out of the backlog for good. They stay in the
    local store, where `speak-stats` still counts them."""
    _stamp(conn, "rejected_at", utterance_ids, rejected_at)
