"""`speak-stats`: the local dashboard, plain SQL over the raw tables.

Renders in every mode that collects (spec 11: "`speak-stats` renders the local
store in every mode that collects, so a user who never uploads still gets their
own dashboard"). Text output for now; `speak-telemetry --id` is a later command.
"""

import os
import sqlite3
import sys

from speak.telemetry import consent, store
from speak.telemetry.contract import SCHEMA


def _connect(env: dict[str, str], *, host_platform: str) -> sqlite3.Connection:
    path = consent.config_dir(env, host_platform=host_platform) / "telemetry.db"
    return store.connect(path, SCHEMA)


def render(conn: sqlite3.Connection) -> str:
    lines: list[str] = []

    (total,) = conn.execute("SELECT COUNT(*) FROM utterances").fetchone()
    if total == 0:
        return "No utterances recorded yet."

    (unshipped,) = conn.execute(f"SELECT COUNT(*) FROM utterances WHERE {store.BACKLOG}").fetchone()
    (rejected,) = conn.execute(
        "SELECT COUNT(*) FROM utterances WHERE rejected_at IS NOT NULL"
    ).fetchone()
    (sessions,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    summary = f"{total} utterances across {sessions} sessions; {unshipped} unshipped"
    # Refused by the server and never resent: kept here so a rejection is visible.
    summary += f", {rejected} rejected by the server." if rejected else "."
    lines.append(summary)

    lines.append("")
    lines.append("By direction and status:")
    for direction, status, count in conn.execute(
        "SELECT direction, status, COUNT(*) AS n FROM utterances "
        "GROUP BY direction, status ORDER BY direction, n DESC"
    ):
        lines.append(f"  {direction:>3} {status:<14} {count}")

    lines.append("")
    lines.append("Speaking, by intent:")
    for intent, count in conn.execute(
        "SELECT COALESCE(intent, 'unspecified'), COUNT(*) AS n FROM utterances "
        "WHERE direction = 'out' GROUP BY intent ORDER BY n DESC"
    ):
        lines.append(f"  {intent:<14} {count}")

    lines.append("")
    lines.append("Interruption rate by intent (interrupted / total, direction=out):")
    for intent, interrupted, count in conn.execute(
        "SELECT COALESCE(intent, 'unspecified'), "
        "SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END), COUNT(*) AS n "
        "FROM utterances WHERE direction = 'out' GROUP BY intent ORDER BY n DESC"
    ):
        rate = (interrupted / count * 100) if count else 0.0
        lines.append(f"  {intent:<14} {interrupted}/{count} ({rate:.0f}%)")

    lines.append("")
    lines.append("Synthesis latency (synth_ms), by engine:")
    for engine, avg_ms, n in conn.execute(
        "SELECT COALESCE(engine, 'unknown'), AVG(synth_ms), COUNT(synth_ms) AS n "
        "FROM utterances WHERE direction = 'out' AND synth_ms IS NOT NULL "
        "GROUP BY engine ORDER BY n DESC"
    ):
        lines.append(f"  {engine:<10} avg {avg_ms:.0f}ms over {n} utterances")

    (heard_nothing,) = conn.execute(
        "SELECT COUNT(*) FROM utterances WHERE direction = 'in' AND status = 'heard_nothing'"
    ).fetchone()
    (in_total,) = conn.execute("SELECT COUNT(*) FROM utterances WHERE direction = 'in'").fetchone()
    if in_total:
        lines.append("")
        lines.append(f"Voice input: {in_total} recordings, {heard_nothing} heard nothing.")

    return "\n".join(lines)


def main() -> None:
    env = dict(os.environ)
    path = consent.config_dir(env, host_platform=sys.platform) / "telemetry.db"
    if not path.exists():
        print("No telemetry recorded yet.")
        return
    conn = _connect(env, host_platform=sys.platform)
    try:
        print(render(conn))
        c = consent.ConsentStore(
            consent.config_dir(env, host_platform=sys.platform) / consent.CONFIG_FILENAME
        ).load()
        if c is not None:
            print(f"\ninstall id: {c.install_id}")
            print(f"mode: {c.mode}" + (" (recording audio)" if c.record else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
