"""The store and the shipper know nothing about utterances: a plugin whose rows are
jobs stores, ships and sets aside its rows through the same code."""

import json
from pathlib import Path

from telemetry_fixtures import JOBS

from speak.telemetry import ship, store

URL = "https://telemetry.example/v1/ingest"
INSTALL = {
    "install_id": "install-1",
    "first_seen_at": "2026-10-06T10:00:00Z",
    "last_seen_at": "2026-10-06T10:00:00Z",
    "plugin_version": "1.0.0",
    "os": "darwin",
    "arch": "arm64",
    "accel": "metal",
}
SESSION = {
    "session_id": "session-1",
    "install_id": "install-1",
    "started_at": "2026-10-06T10:00:00Z",
    "plugin_version": "1.0.0",
    "core_version": "0.1.0",
    "harness": "claude-code",
    "ffmpeg_present": 1,
}


def job(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "job_id": "job-1",
        "session_id": "session-1",
        "install_id": "install-1",
        "kind": "transcription",
        "started_at": "2026-10-06T10:00:00Z",
        "ended_at": "2026-10-06T10:01:00Z",
        "status": "ok",
        "has_video": 0,
        "error_message": "/Users/x/private.mov: fine",
    }
    row.update(overrides)
    return row


def seeded(rows, *, backlog_cap: int = store.DEFAULT_BACKLOG_CAP):
    conn = store.connect(Path(":memory:"), JOBS)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, JOBS, SESSION)
    for row in rows:
        store.insert_row(conn, JOBS.rows, row, backlog_cap=backlog_cap)
    return conn


class Transport:
    def __init__(self, status: int) -> None:
        self.status = status
        self.bodies: list[dict[str, object]] = []

    async def __call__(self, url: str, body: bytes) -> ship.TransportResponse:
        self.bodies.append(json.loads(body))
        return ship.TransportResponse(status=self.status, body=b'{"accepted": 2}')


async def no_sleep(_: float) -> None:
    return None


async def test_jobs_ship_under_their_own_key_without_their_local_columns():
    conn = seeded([job(job_id="j1"), job(job_id="j2", has_video=1)])
    transport = Transport(202)

    shipped = await ship.ship(
        conn, schema=JOBS, install_id="install-1", url=URL, transport=transport, sleep=no_sleep
    )

    assert shipped == 2
    (body,) = transport.bodies
    assert set(body) == {"install", "sessions", "jobs"}
    assert [row["job_id"] for row in body["jobs"]] == ["j1", "j2"]
    for row in body["jobs"]:
        assert not {"error_message", "shipped_at", "rejected_at"} & set(row)
    assert body["jobs"][1]["has_video"] is True
    assert body["sessions"][0]["ffmpeg_present"] is True
    assert store.unshipped_rows(conn, JOBS.rows) == []


async def test_a_refused_chunk_of_jobs_is_set_aside():
    conn = seeded([job(job_id="j1"), job(job_id="j2")])

    await ship.ship(
        conn, schema=JOBS, install_id="install-1", url=URL, transport=Transport(400), sleep=no_sleep
    )

    assert store.unshipped_rows(conn, JOBS.rows) == []
    rejected = conn.execute("SELECT COUNT(*) FROM jobs WHERE rejected_at IS NOT NULL").fetchone()
    assert rejected[0] == 2


def test_a_table_without_a_sequence_column_inserts_plainly():
    conn = seeded([job()])
    assert dict(conn.execute("SELECT job_id, kind FROM jobs").fetchone()) == {
        "job_id": "job-1",
        "kind": "transcription",
    }


def test_the_backlog_cap_drops_the_oldest_jobs():
    conn = seeded(
        [
            job(job_id="old", ended_at="2026-10-06T10:01:00Z"),
            job(job_id="mid", ended_at="2026-10-06T10:02:00Z"),
            job(job_id="new", ended_at="2026-10-06T10:03:00Z"),
        ],
        backlog_cap=2,
    )
    assert [row["job_id"] for row in store.unshipped_rows(conn, JOBS.rows)] == ["mid", "new"]
