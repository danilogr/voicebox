"""Projects the local backlog to the wire format and POSTs it.

Runs once per session, in the background, off the hot path (spec 7). Every failure
here leaves the affected rows unshipped; the next session's attempt is the entire
retry mechanism (spec 11) - there is no separate queue to keep in step with the
local store.

Takes an injected transport, so no test here opens a socket.
"""

import asyncio
import json
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from speak.telemetry import store

logger = logging.getLogger(__name__)

# Sized well under the server's 64 KB body cap (spec 4.2), leaving room for the
# request envelope around the utterances this budgets.
CHUNK_ROWS = 100
CHUNK_BYTES = 48_000

MAX_ATTEMPTS = 5
BASE_BACKOFF = 1.0

# Local-only per spec 1 ("two columns that must not be uploaded") plus the rest of
# the LOCAL-tagged columns in contract/v1/schema.sql. `installs` and `sessions` have
# no local-only columns at all, so only `utterances` needs a projection.
LOCAL_ONLY_UTTERANCE_COLUMNS = frozenset(
    {
        "text",
        "text_sha256",
        "audio_path",
        "audio_sha256",
        "device_name",
        "corrected_text",
        "corrected_at",
        "correction_kind",
        "shipped_at",
    }
)

# SQLite stores these as 0/1; the contract types them `boolean`.
_BOOLEAN_UTTERANCE_COLUMNS = ("converted", "diarize")

# `install` never carries `country`: the server derives it from CF-IPCountry on
# receipt, and the client never collects or sends it (spec 1.2, 4.5).
_INSTALL_UPLOAD_FIELDS = (
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

_SESSION_UPLOAD_FIELDS = (
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


# The contract's enums for the two required install fields that come straight from
# the platform. Neither has a value for "something else", so an install on any
# other OS or architecture cannot be sent at all: every chunk would carry an install
# the server refuses. Its rows stay local, where `speak-stats` still shows them.
CONTRACT_OS = frozenset({"darwin", "linux", "win32"})
CONTRACT_ARCH = frozenset({"arm64", "x86_64"})


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    body: bytes = b""


Transport = Callable[[str, bytes], Awaitable[TransportResponse]]
Sleep = Callable[[float], Awaitable[None]]


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Drop `None`s: no property in the wire schema accepts `null`."""
    return {k: v for k, v in row.items() if v is not None}


def _project_install(row: dict[str, Any]) -> dict[str, Any]:
    return _clean({k: row[k] for k in _INSTALL_UPLOAD_FIELDS if k in row})


def _project_session(row: dict[str, Any]) -> dict[str, Any]:
    projected = _clean({k: row[k] for k in _SESSION_UPLOAD_FIELDS if k in row})
    if "listen_enabled" in projected:
        projected["listen_enabled"] = bool(projected["listen_enabled"])
    return projected


def _project_utterance(row: dict[str, Any]) -> dict[str, Any]:
    projected = _clean({k: v for k, v in row.items() if k not in LOCAL_ONLY_UTTERANCE_COLUMNS})
    for column in _BOOLEAN_UTTERANCE_COLUMNS:
        if column in projected:
            projected[column] = bool(projected[column])
    return projected


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Split rows at 100 items or ~48 KB of projected JSON, whichever comes first."""
    chunk: list[dict[str, Any]] = []
    size = 2  # "[]"
    for row in rows:
        encoded_len = len(json.dumps(_project_utterance(row))) + 1
        if chunk and (len(chunk) >= CHUNK_ROWS or size + encoded_len > CHUNK_BYTES):
            yield chunk
            chunk, size = [], 2
        chunk.append(row)
        size += encoded_len
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
) -> bool:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await transport(url, body)
        except OSError as error:
            logger.debug("telemetry request failed: %s", error)
            if attempt == MAX_ATTEMPTS - 1:
                return False
            await sleep(_backoff(attempt, rng))
            continue

        if response.status == 202:
            # `accepted` may be less than `expected`: a soft-dropped quota, not a
            # partial failure. The whole chunk is marked shipped either way.
            _log_quota_shortfall(response.body, expected)
            return True
        if not _should_retry(response.status):
            logger.debug("telemetry chunk rejected with status %d", response.status)
            return False
        if attempt == MAX_ATTEMPTS - 1:
            return False
        await sleep(_backoff(attempt, rng))
    return False


async def ship(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    transport: Transport,
    debug: bool = False,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> int:
    """Ship whatever is unshipped. Returns how many utterance rows were marked shipped.

    `debug` logs each chunk instead of sending it and marks nothing shipped - a dry
    run for verifying the projection (`SPEAK_TELEMETRY_DEBUG=1`), not a delivery.
    """
    rows = store.unshipped_utterances(conn)
    if not rows:
        return 0

    install = store.install_row(conn)
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

    rng = rng or random.Random()
    shipped = 0
    sent_sessions: set[str] = set()

    for chunk in _chunks(rows):
        session_ids = sorted({row["session_id"] for row in chunk} - sent_sessions)
        sessions = store.sessions_by_id(conn, session_ids)
        payload = {
            "install": _project_install(install),
            "sessions": [_project_session(s) for s in sessions],
            "utterances": [_project_utterance(row) for row in chunk],
        }

        if debug:
            logger.info("SPEAK_TELEMETRY_DEBUG payload: %s", json.dumps(payload, sort_keys=True))
            sent_sessions.update(session_ids)
            continue

        if not url:
            logger.debug("telemetry: no endpoint configured; leaving %d rows unshipped", len(chunk))
            break

        sent_ok = await _send_chunk(
            payload, url=url, transport=transport, sleep=sleep, rng=rng, expected=len(chunk)
        )
        if not sent_ok:
            break

        sent_sessions.update(session_ids)
        ids = [row["utterance_id"] for row in chunk]
        store.mark_shipped(conn, ids, store.now_iso())
        shipped += len(ids)

    return shipped
