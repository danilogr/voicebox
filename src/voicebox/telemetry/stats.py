"""voicebox-stats: this install's own jobs, plain SQL over the local store."""

import sqlite3
import statistics

from speak_core.telemetry import store

# Model time against the audio it covers, per kind: the realtime factor.
SPEED_COLUMN = {"transcription": "asr_ms", "synthesis": "synth_ms", "narration": "synth_ms"}
RECENT_FAILURES = 5


def _count(conn: sqlite3.Connection, sql: str) -> int:
    (value,) = conn.execute(sql).fetchone()
    return int(value)


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def render(conn: sqlite3.Connection) -> str:
    total = _count(conn, "SELECT COUNT(*) FROM jobs")
    if total == 0:
        return "No jobs recorded yet."
    unshipped = _count(conn, f"SELECT COUNT(*) FROM jobs WHERE {store.BACKLOG}")
    rejected = _count(conn, "SELECT COUNT(*) FROM jobs WHERE rejected_at IS NOT NULL")
    sessions = _count(conn, "SELECT COUNT(*) FROM sessions")
    summary = (
        f"{total} job{_plural(total)} across {sessions} session{_plural(sessions)}; "
        f"{unshipped} unshipped"
    )
    # Refused by the server and never resent: shown so a rejection is visible.
    lines = [summary + (f", {rejected} rejected by the server." if rejected else ".")]

    lines += ["", "By kind and status:"]
    for kind, status, count in conn.execute(
        "SELECT kind, status, COUNT(*) AS n FROM jobs GROUP BY kind, status ORDER BY kind, n DESC"
    ):
        lines.append(f"  {kind:<14} {status:<8} {count}")

    speeds = []
    for kind, column in SPEED_COLUMN.items():
        ratios = [
            model / audio
            for model, audio in conn.execute(
                f"SELECT {column}, audio_ms FROM jobs WHERE kind = ? AND status = 'ok' "
                f"AND {column} IS NOT NULL AND audio_ms > 0",
                (kind,),
            )
        ]
        if ratios:
            plural = "" if len(ratios) == 1 else "s"
            speeds.append(
                f"  {kind:<14} {statistics.median(ratios):.2f}x over {len(ratios)} job{plural}"
            )
    if speeds:
        lines += ["", "Speed (model time / audio time, median; lower is faster):", *speeds]

    failures = conn.execute(
        "SELECT ended_at, kind, status, reason, stage, error_message FROM jobs "
        "WHERE status IN ('failed', 'crashed') ORDER BY ended_at DESC LIMIT ?",
        (RECENT_FAILURES,),
    ).fetchall()
    if failures:
        lines += ["", "Recent failures:"]
        for ended, kind, status, reason, stage, message in failures:
            where = f"{reason or status} at {stage}" if stage else (reason or status)
            lines.append(f"  {ended[:19]}  {kind:<14} {where}: {message or ''}".rstrip())
    return "\n".join(lines)
