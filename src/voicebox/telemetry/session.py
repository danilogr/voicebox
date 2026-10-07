"""One MCP session's telemetry: its row in `sessions`, and the connection its jobs and
narrations are recorded through. Every method catches its own failures."""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from speak_core.telemetry import store

from voicebox.telemetry.record import insert

logger = logging.getLogger(__name__)

# `harness_version` is whatever the MCP client declares; the contract caps its length.
HARNESS_VERSION_MAX_LENGTH = 64


@dataclass(slots=True)
class TelemetrySession:
    conn: sqlite3.Connection
    install_id: str
    session_id: str
    db_path: Path

    def ref(self) -> dict[str, str]:
        """What a detached worker needs to record a job for this session after it ends."""
        return {
            "db_path": str(self.db_path),
            "session_id": self.session_id,
            "install_id": self.install_id,
        }

    def record(self, row: dict[str, object]) -> None:
        insert(self.conn, row)

    def close(self) -> None:
        try:
            store.end_session(self.conn, self.session_id, store.now_iso())
        except sqlite3.Error:
            logger.debug("telemetry: could not close the session row", exc_info=True)

    def update_harness(self, harness: str, harness_version: str | None) -> None:
        """Correct the harness the row was opened with, once the MCP client's own
        `client_info` - the better source - is known."""
        if harness_version is not None:
            harness_version = harness_version[:HARNESS_VERSION_MAX_LENGTH]
        try:
            store.update_session_harness(self.conn, self.session_id, harness, harness_version)
        except sqlite3.Error:
            logger.debug("telemetry: could not update the session's harness", exc_info=True)
