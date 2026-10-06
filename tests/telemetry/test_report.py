"""The stats scaffolding every plugin's `<plugin>-stats` uses: find the store, render
it with the plugin's own function, and say which install and mode it belongs to."""

import io
import sqlite3
from pathlib import Path

from telemetry_fixtures import IDENTITY, UTTERANCES

from speak.telemetry import consent, report, store


def render(conn: sqlite3.Connection) -> str:
    (sessions,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return f"{sessions} sessions"


def run(tmp_path: Path, db_path: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = report.run(
        IDENTITY,
        {"HOME": str(tmp_path)},
        host_platform="linux",
        schema=UTTERANCES,
        db_path=db_path,
        render=render,
        out=out,
    )
    return code, out.getvalue()


def test_no_store_prints_a_plain_message(tmp_path):
    code, out = run(tmp_path, tmp_path / "missing.db")
    assert code == 0
    assert out == "No telemetry recorded yet.\n"


def test_the_plugins_render_is_printed_with_the_install_and_mode(tmp_path):
    db = tmp_path / "telemetry.db"
    store.connect(db, UTTERANCES).close()
    env = {"HOME": str(tmp_path)}
    consented = consent.load_consent(env, IDENTITY, host_platform="linux")
    code, out = run(tmp_path, db)
    assert code == 0
    assert out == f"0 sessions\n\ninstall id: {consented.install_id}\nmode: on\n"
