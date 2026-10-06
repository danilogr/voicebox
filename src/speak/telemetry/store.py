"""Local SQLite: a plugin's telemetry schema, and plain CRUD over it.

The schema is the plugin's (its contract's `schema.sql` and the `TelemetrySchema`
naming its columns); this module handles the shipped row table - `utterances` for one
plugin, `jobs` for another - without knowing what is in it. Table and column names are
interpolated into SQL; they come from a `TelemetrySchema` defined in code, never from
input.

The local database is the outbound queue, so there is one mechanism rather than a
database plus a spool file. A row is in the backlog until it is shipped or the
server refuses it (`shipped_at` and `rejected_at` both NULL); a session is until it
has been sent as it stands now (`shipped_at` NULL, cleared by every update to it).

D1 enforces foreign keys on every query, unconditionally; Python's `sqlite3` defaults
`PRAGMA foreign_keys` to off, per connection. Every connection this module opens turns
it on, so an orphaned row is refused locally the same way it would be in production -
without this, local tests pass on rows production rejects (spec 4.8).
"""

import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from speak.telemetry.schema import RowTable, TelemetrySchema

logger = logging.getLogger(__name__)

# Tables with more unshipped rows than this are pruned, oldest first, so a
# server that becomes permanently unreachable cannot grow the local file forever.
DEFAULT_BACKLOG_CAP = 10_000

# The backlog: neither shipped nor refused by the server. One definition, shared by
# the read, the cap and the plugin's stats.
BACKLOG = "shipped_at IS NULL AND rejected_at IS NULL"

INSTALL_COLUMNS = (
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


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _idempotent(sql: str) -> str:
    """`schema.sql` runs once against a fresh D1 database and has no `IF NOT EXISTS`.

    The local file persists across every session, so creating its tables has to be
    idempotent without changing what they end up looking like.
    """
    sql = re.sub(r"CREATE TABLE (\w)", r"CREATE TABLE IF NOT EXISTS \1", sql)
    return re.sub(r"CREATE INDEX (\w)", r"CREATE INDEX IF NOT EXISTS \1", sql)


def connect(path: Path, schema: TelemetrySchema) -> sqlite3.Connection:
    """Open the local store, creating `schema`'s tables and the directory if needed.

    `PRAGMA foreign_keys = ON` is set here and nowhere else needs to remember to:
    every connection this module hands out is safe by construction.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_idempotent(schema.sql))
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
    _upsert(conn, "installs", INSTALL_COLUMNS, row)


def install_row(conn: sqlite3.Connection, install_id: str | None = None) -> dict[str, Any] | None:
    if install_id is None:
        cursor = conn.execute("SELECT * FROM installs LIMIT 1")
    else:
        cursor = conn.execute("SELECT * FROM installs WHERE install_id = ?", (install_id,))
    row = cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


def insert_session(conn: sqlite3.Connection, schema: TelemetrySchema, row: dict[str, Any]) -> None:
    """Record one session. `session_id` is generated fresh per process, so a plain
    insert is enough - there is nothing to conflict with."""
    present = [c for c in schema.session_columns if c in row]
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


def insert_row(
    conn: sqlite3.Connection,
    table: RowTable,
    row: dict[str, Any],
    *,
    backlog_cap: int = DEFAULT_BACKLOG_CAP,
) -> None:
    """Insert one row and enforce the unshipped-backlog cap.

    A table with a `seq_column` has it assigned here and nowhere else, inside the
    INSERT itself, as one more than the session's highest so far: a worker process
    writes to the same session as the server, so a counter held by either would
    collide with the other's rows, and one statement under SQLite's single write lock
    cannot. Any value the caller passes for it is ignored.

    A missing `session_id` (or one that names no session) is refused by the foreign
    key, exactly as D1 refuses it in production.
    """
    seq = table.seq_column
    present = [c for c in table.columns if c in row and c != seq]
    placeholders = ", ".join(f":{c}" for c in present)
    if seq is None:
        sql = f"INSERT INTO {table.name} ({', '.join(present)}) VALUES ({placeholders})"
    else:
        sql = (
            f"INSERT INTO {table.name} ({', '.join(present)}, {seq}) VALUES ({placeholders}, "
            f"(SELECT COALESCE(MAX({seq}), 0) + 1 FROM {table.name} "
            "WHERE session_id = :session_id))"
        )
    conn.execute(sql, row)
    conn.commit()
    _enforce_backlog_cap(conn, table, backlog_cap)


def _enforce_backlog_cap(conn: sqlite3.Connection, table: RowTable, cap: int) -> None:
    (count,) = conn.execute(f"SELECT COUNT(*) FROM {table.name} WHERE {BACKLOG}").fetchone()
    over = count - cap
    if over <= 0:
        return
    conn.execute(
        f"""
        DELETE FROM {table.name} WHERE {table.id_column} IN (
            SELECT {table.id_column} FROM {table.name}
            WHERE {BACKLOG}
            ORDER BY {table.order_column} ASC, rowid ASC
            LIMIT ?
        )
        """,
        (over,),
    )
    conn.commit()
    logger.debug("telemetry backlog over %d rows; dropped the oldest %d", cap, over)


def unshipped_rows(
    conn: sqlite3.Connection, table: RowTable, install_id: str | None = None
) -> list[dict[str, Any]]:
    """The backlog, oldest first - one install's, or every install's when `None`.

    Bounded by construction: `insert_row` never lets the backlog grow past its cap.
    """
    where = BACKLOG + ("" if install_id is None else " AND install_id = ?")
    cursor = conn.execute(
        f"SELECT * FROM {table.name} WHERE {where} ORDER BY {table.order_column} ASC, rowid ASC",
        () if install_id is None else (install_id,),
    )
    return [_row_to_dict(row) for row in cursor.fetchall()]


def _stamp(
    conn: sqlite3.Connection, table: RowTable, column: str, ids: Sequence[str], at: str
) -> None:
    if not ids:
        return
    conn.executemany(
        f"UPDATE {table.name} SET {column} = ? WHERE {table.id_column} = ?",
        [(at, row_id) for row_id in ids],
    )
    conn.commit()


def mark_shipped(
    conn: sqlite3.Connection, table: RowTable, ids: Sequence[str], shipped_at: str
) -> None:
    _stamp(conn, table, "shipped_at", ids, shipped_at)


def mark_rejected(
    conn: sqlite3.Connection, table: RowTable, ids: Sequence[str], rejected_at: str
) -> None:
    """Take rows the server refused out of the backlog for good. They stay in the
    local store, where the plugin's stats still count them."""
    _stamp(conn, table, "rejected_at", ids, rejected_at)
