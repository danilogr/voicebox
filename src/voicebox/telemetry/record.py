"""Storing a job row - from the session's own connection, or from a worker process that
opens the store by path because the session that asked may be long gone.

Never raises: a dead telemetry backend has to be indistinguishable from a healthy one.
Every row goes in insert-if-absent, because the job id is the contract's idempotency key
and a crashed job can be found by more than one process.
"""

import logging
import sqlite3
from pathlib import Path

from speak_core.telemetry import store

from voicebox.telemetry.contract import SCHEMA

logger = logging.getLogger(__name__)


def insert(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    try:
        store.insert_row(conn, SCHEMA.rows, row, if_absent=True)
    except sqlite3.Error:
        logger.debug("telemetry: could not record a job", exc_info=True)


def insert_at(db_path: Path, row: dict[str, object]) -> None:
    try:
        conn = store.connect(db_path, SCHEMA)
    except (OSError, sqlite3.Error) as error:
        logger.debug("telemetry: could not open the local store: %s", error)
        return
    try:
        insert(conn, row)
    finally:
        conn.close()
