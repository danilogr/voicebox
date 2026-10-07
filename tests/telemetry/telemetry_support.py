"""A local store with one install and one session, and the check every client-produced
row must pass: stored, read back, projected, and valid against the vendored schema."""

import json
import sqlite3
from pathlib import Path

import jsonschema
from speak_core.telemetry import ship, store

from voicebox.telemetry.contract import JOBS, SCHEMA

INSTALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CONTRACT = Path(__file__).parents[1] / "contract" / "v1" / "schema" / "ingest-request.schema.json"
REQUEST = jsonschema.Draft202012Validator(json.loads(CONTRACT.read_text()))


def opened(path: Path) -> sqlite3.Connection:
    conn = store.connect(path, SCHEMA)
    store.upsert_install(
        conn,
        {
            "install_id": INSTALL,
            "first_seen_at": "2026-10-06T10:00:00Z",
            "last_seen_at": "2026-10-06T10:00:00Z",
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
            "started_at": "2026-10-06T10:00:00Z",
            "plugin_version": "1.0.0",
            "core_version": "0.3.0",
            "harness": "claude-code",
            "ffmpeg_present": True,
        },
    )
    return conn


def ref(db: Path) -> dict[str, str]:
    return {"db_path": str(db), "session_id": SESSION, "install_id": INSTALL}


def stored_rows(db: Path) -> list[dict[str, object]]:
    conn = store.connect(db, SCHEMA)
    try:
        return store.unshipped_rows(conn, JOBS)
    finally:
        conn.close()


def assert_shippable(db: Path) -> list[dict[str, object]]:
    """Every row in `db`, as the shipper would send it; fails unless all validate."""
    conn = store.connect(db, SCHEMA)
    try:
        install = store.install_row(conn, INSTALL)
        assert install is not None
        jobs = [ship.project_row(JOBS, r) for r in store.unshipped_rows(conn, JOBS)]
        payload = {
            "install": ship.project_install(install),
            "sessions": [
                ship.project_session(SCHEMA, s) for s in store.sessions_by_id(conn, [SESSION])
            ],
            "jobs": jobs,
        }
    finally:
        conn.close()
    errors = list(REQUEST.iter_errors(payload))
    assert errors == [], [e.message for e in errors]
    return jobs
