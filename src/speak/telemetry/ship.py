"""Projects the local backlog to the wire format and POSTs it.

Runs once per session, in the background, off the hot path (spec 7). A failure to
deliver leaves the affected rows unshipped; the next session's attempt is the entire
retry mechanism (spec 11) - there is no separate queue to keep in step with the
local store. A chunk the server refuses as malformed (`400`) is set aside instead,
so one bad row cannot hold back every row after it.

What ships is this install's backlog of the schema's row table, then any of its
sessions that are new or changed and were not already carried by a chunk of rows - a
session with no rows, or one whose `ended_at` was written after its rows shipped. The
schema says which columns stay local, which are booleans on the wire, and the key the
rows travel under.

Takes an injected transport, so no test here opens a socket.
"""

import asyncio
import json
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from speak.telemetry import store
from speak.telemetry.schema import RowTable, TelemetrySchema

logger = logging.getLogger(__name__)

# Sized well under the server's 64 KB body cap (spec 4.2), leaving room for the
# request envelope around the rows this budgets.
CHUNK_ROWS = 100
CHUNK_BYTES = 48_000

MAX_ATTEMPTS = 5
BASE_BACKOFF = 1.0

# `install` never carries `country`: the server derives it from CF-IPCountry on
# receipt, and the client never collects or sends it (spec 1.2, 4.5).
INSTALL_UPLOAD_FIELDS = (
    "install_id",
    "first_seen_at",
    "last_seen_at",
    "plugin_version",
    "disclosure_ver",
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

# The enums every plugin's contract shares for the two required install fields that come
# straight from the platform. Neither has a value for "something else", so an install on any
# other OS or architecture cannot be sent at all: every chunk would carry an install
# the server refuses. Its rows stay local, where the plugin's stats still show them.
CONTRACT_OS = frozenset({"darwin", "linux", "win32"})
CONTRACT_ARCH = frozenset({"arm64", "x86_64"})


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    body: bytes = b""


Transport = Callable[[str, bytes], Awaitable[TransportResponse]]
Sleep = Callable[[float], Awaitable[None]]


class Delivery(Enum):
    ACCEPTED = "accepted"  # 202: mark it shipped
    REJECTED = "rejected"  # 400: the payload is the problem; set it aside, go on
    FAILED = "failed"  # anything else, after retries: stop, the next session retries


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Drop `None`s: no property in the wire schema accepts `null`."""
    return {k: v for k, v in row.items() if v is not None}


def project_install(row: dict[str, Any]) -> dict[str, Any]:
    return _clean({k: row[k] for k in INSTALL_UPLOAD_FIELDS if k in row})


def project_session(schema: TelemetrySchema, row: dict[str, Any]) -> dict[str, Any]:
    projected = _clean({k: row[k] for k in schema.session_columns if k in row})
    for column in schema.session_boolean_columns:
        if column in projected:
            projected[column] = bool(projected[column])
    return projected


def project_row(table: RowTable, row: dict[str, Any]) -> dict[str, Any]:
    projected = _clean({k: v for k, v in row.items() if k not in table.local_columns})
    for column in table.boolean_columns:
        if column in projected:
            projected[column] = bool(projected[column])
    return projected


def _encoded_len(projected: dict[str, Any]) -> int:
    return len(json.dumps(projected)) + 1  # the comma that separates it


def _chunks(
    rows: Sequence[dict[str, Any]], sessions: Mapping[str, dict[str, Any]], schema: TelemetrySchema
) -> Iterator[list[dict[str, Any]]]:
    """Split rows at 100 items or ~48 KB of projected JSON, whichever comes first.

    The budget counts every distinct session the chunk's rows belong to, since each
    travels with them: a backlog of single-row sessions puts a session
    beside nearly every row, and a budget of the rows alone would build a request over
    the server's body cap.
    """
    chunk: list[dict[str, Any]] = []
    chunk_sessions: set[str] = set()
    size = 2  # "[]"
    for row in rows:
        session_id = row["session_id"]
        row_cost = _encoded_len(project_row(schema.rows, row))
        session = sessions.get(session_id)
        session_cost = _encoded_len(project_session(schema, session)) if session is not None else 0
        cost = row_cost + (0 if session_id in chunk_sessions else session_cost)
        if chunk and (len(chunk) >= CHUNK_ROWS or size + cost > CHUNK_BYTES):
            yield chunk
            chunk, chunk_sessions, size = [], set(), 2
            cost = row_cost + session_cost
        chunk.append(row)
        chunk_sessions.add(session_id)
        size += cost
    if chunk:
        yield chunk


def _session_chunks(
    sessions: Sequence[dict[str, Any]], schema: TelemetrySchema
) -> Iterator[list[dict[str, Any]]]:
    """Sessions with no row to carry them, at 100 or ~48 KB per request."""
    chunk: list[dict[str, Any]] = []
    size = 2
    for session in sessions:
        cost = _encoded_len(project_session(schema, session))
        if chunk and (len(chunk) >= CHUNK_ROWS or size + cost > CHUNK_BYTES):
            yield chunk
            chunk, size = [], 2
        chunk.append(session)
        size += cost
    if chunk:
        yield chunk


def _should_retry(status: int) -> bool:
    return status == 429 or status >= 500


def _backoff(attempt: int, rng: random.Random) -> float:
    return float(BASE_BACKOFF * (2**attempt) * (0.5 + rng.random()))


def _log_quota_shortfall(response_body: bytes, expected: int) -> None:
    """A `202` whose `accepted` is under `expected` is the per-install quota
    soft-dropping the excess, not a failure (CONTRACT.md, "the per-install quota
    does not fail the request"). It is never retried and never leaves rows
    unshipped - only logged, so a quota that is biting is still diagnosable.
    """
    try:
        accepted = json.loads(response_body).get("accepted")
    except (json.JSONDecodeError, AttributeError):
        return
    if isinstance(accepted, int) and accepted < expected:
        logger.debug(
            "telemetry: server accepted %d of %d rows in this chunk (per-install quota)",
            accepted,
            expected,
        )


async def _send_chunk(
    payload: dict[str, Any],
    *,
    url: str,
    transport: Transport,
    sleep: Sleep,
    rng: random.Random,
    expected: int,
) -> Delivery:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await transport(url, body)
        except OSError as error:
            logger.debug("telemetry request failed: %s", error)
            if attempt == MAX_ATTEMPTS - 1:
                return Delivery.FAILED
            await sleep(_backoff(attempt, rng))
            continue

        if response.status == 202:
            # `accepted` may be less than `expected`: a soft-dropped quota, not a
            # partial failure. The whole chunk is marked shipped either way.
            _log_quota_shortfall(response.body, expected)
            return Delivery.ACCEPTED
        if response.status == 400:
            logger.debug("telemetry chunk refused as malformed: %s", response.body[:500])
            return Delivery.REJECTED
        if not _should_retry(response.status):
            logger.debug("telemetry chunk not delivered: status %d", response.status)
            return Delivery.FAILED
        if attempt == MAX_ATTEMPTS - 1:
            return Delivery.FAILED
        await sleep(_backoff(attempt, rng))
    return Delivery.FAILED


async def ship(
    conn: sqlite3.Connection,
    *,
    schema: TelemetrySchema,
    install_id: str,
    url: str | None,
    transport: Transport,
    debug: bool = False,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> int:
    """Ship this install's backlog. Returns how many rows were marked shipped.

    Only rows recorded under `install_id` go out, under that install's own row: the
    server refuses a session whose install is not the request's, and rows left from
    an earlier install id stay local rather than being sent under a new one.

    `debug` logs each chunk instead of sending it and marks nothing - a dry run for
    verifying the projection, not a delivery.
    """
    install = store.install_row(conn, install_id)
    if install is None:
        logger.debug("telemetry: no install row recorded yet; nothing to ship")
        return 0
    if install["os"] not in CONTRACT_OS or install["arch"] not in CONTRACT_ARCH:
        logger.debug(
            "telemetry: %s/%s is outside the contract; keeping every row local",
            install["os"],
            install["arch"],
        )
        return 0

    rows = store.unshipped_rows(conn, schema.rows, install_id)
    changed = store.unshipped_sessions(conn, install_id)
    if not rows and not changed:
        return 0

    sessions = {s["session_id"]: s for s in store.sessions_by_id(conn, _session_ids(rows))}
    sessions.update((s["session_id"], s) for s in changed)
    sender = _Sender(
        conn,
        schema=schema,
        install=project_install(install),
        url=url,
        transport=transport,
        debug=debug,
        sleep=sleep,
        rng=rng or random.Random(),
    )

    for chunk in _chunks(rows, sessions, schema):
        # Every chunk carries every session its rows belong to, even one an earlier
        # chunk of this run carried: that one may have been soft-dropped by the
        # server's quota, and a row whose session the server lacks is refused.
        carried = [sessions[i] for i in _session_ids(chunk)]
        if not await sender.send(carried, chunk):
            return sender.shipped

    alone = [s for s in changed if s["session_id"] not in sender.delivered]
    for group in _session_chunks(alone, schema):
        if not await sender.send(group, []):
            break
    return sender.shipped


def _session_ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted({row["session_id"] for row in rows})


class _Sender:
    """One shipping run's requests, and what each outcome does to the local store."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        schema: TelemetrySchema,
        install: dict[str, Any],
        url: str | None,
        transport: Transport,
        debug: bool,
        sleep: Sleep,
        rng: random.Random,
    ) -> None:
        self._conn = conn
        self._schema = schema
        self._install = install
        self._url = url
        self._transport = transport
        self._debug = debug
        self._sleep = sleep
        self._rng = rng
        # Sessions delivered in this run, so none is sent again on its own.
        self.delivered: set[str] = set()
        self.shipped = 0

    async def send(self, sessions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bool:
        """Send one request. False means stop: nothing later can be delivered either."""
        table = self._schema.rows
        payload = {
            "install": self._install,
            "sessions": [project_session(self._schema, s) for s in sessions],
            table.name: [project_row(table, row) for row in rows],
        }
        if self._debug:
            logger.info("telemetry debug, not sent: %s", json.dumps(payload, sort_keys=True))
            self.delivered.update(s["session_id"] for s in sessions)
            return True
        if not self._url:
            logger.debug("telemetry: no endpoint configured; leaving the backlog unshipped")
            return False

        delivery = await _send_chunk(
            payload,
            url=self._url,
            transport=self._transport,
            sleep=self._sleep,
            rng=self._rng,
            expected=len(rows),
        )
        now = store.now_iso()
        if delivery is Delivery.REJECTED:
            logger.debug(
                "telemetry: the server refused %d row(s) and %d session(s); setting them aside",
                len(rows),
                len(sessions),
            )
            if rows:
                # Its sessions stay in the backlog: they were not stored, and the fault
                # may be in the rows alone, so they go out again by themselves.
                store.mark_rejected(self._conn, table, [row[table.id_column] for row in rows], now)
            else:
                # Sessions refused on their own are the fault. Marked as sent, they
                # leave the backlog instead of being refused again every session; a
                # row of theirs still carries them, since every chunk of rows carries
                # the sessions it references.
                store.mark_sessions_shipped(self._conn, sessions, now)
            return True
        if delivery is Delivery.FAILED:
            return False

        self.delivered.update(s["session_id"] for s in sessions)
        store.mark_sessions_shipped(self._conn, sessions, now)
        store.mark_shipped(self._conn, table, [row[table.id_column] for row in rows], now)
        self.shipped += len(rows)
        return True
