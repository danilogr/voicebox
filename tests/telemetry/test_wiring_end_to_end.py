"""`__main__.build()` wires the notice, the session row and harness correction; only a
real subprocess runs its lifespan the way `voicebox-mcp` does."""

import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import mcp_types
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from voicebox.telemetry.notice import TEXT

# The consent and notice files are read at the macOS config path below.
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS config directory")


def env_for(tmp_path: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "VOICEBOX_STATE_DIR": str(tmp_path / "state"),
        "SPEAK_CORE_STATE_DIR": str(tmp_path / "core"),
        "VOICEBOX_ENGINE": "command",
        "VOICEBOX_COMMAND": "true",
        "NEMO_AUTOSTART": "0",
        # Collecting needs DO_NOT_TRACK cleared; debug turns the ping and any shipping
        # into a log line instead of a request.
        "DO_NOT_TRACK": "",
        "VOICEBOX_TELEMETRY_DEBUG": "1",
    }
    env.pop("VOICEBOX_TELEMETRY", None)
    return env


@asynccontextmanager
async def connected(tmp_path: Path, client_info: mcp_types.Implementation | None = None):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "voicebox"], env=env_for(tmp_path), cwd=str(Path.cwd())
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, client_info=client_info) as session,
    ):
        await session.initialize()
        yield session


def notice_marker(tmp_path: Path) -> Path:
    return tmp_path / "home" / "Library" / "Application Support" / "voicebox" / "notice.json"


def pre_mark_notice(tmp_path: Path) -> None:
    notice_marker(tmp_path).parent.mkdir(parents=True)
    notice_marker(tmp_path).write_text(json.dumps({"disclosure_ver": 1}))


async def text_of(session, name: str, **arguments: object) -> str:
    return (await session.call_tool(name, arguments)).content[0].text


async def test_a_first_session_shows_the_notice_once_and_collects_nothing(tmp_path):
    async with connected(tmp_path) as session:
        first = await text_of(session, "job_status")
        second = await text_of(session, "job_status")
    assert TEXT in first and TEXT not in second
    assert not (tmp_path / "state" / "telemetry.db").exists()
    assert json.loads(notice_marker(tmp_path).read_text()) == {"disclosure_ver": 1}


async def test_once_the_notice_was_shown_a_session_row_is_written(tmp_path):
    pre_mark_notice(tmp_path)
    info = mcp_types.Implementation(name="antigravity-client", version="1.4.0")
    async with connected(tmp_path, client_info=info) as session:
        reply = await text_of(session, "job_status")
    assert TEXT not in reply
    conn = sqlite3.connect(tmp_path / "state" / "telemetry.db")
    (row,) = conn.execute(
        "SELECT harness, harness_version, ended_at, core_version FROM sessions"
    ).fetchall()
    assert row[0:2] == ("antigravity", "1.4.0")
    assert row[2] is not None and row[3]
