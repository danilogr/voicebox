"""`speak.__main__.build()` is the one thing no other test exercises directly: it
is what actually wires the queue's `on_finish` into a real `TelemetrySession` and
hands one to `create_server`. A real subprocess is the only way to run its
lifespan (which starts the queue's worker) the way `speak-mcp` really does, the
same reason `tests/test_server.py` uses one.
"""

import asyncio
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from speak_core.telemetry import store

from speak.telemetry.contract import SCHEMA


def _env(state_dir: str, home: str) -> dict[str, str]:
    return {
        **os.environ,
        "SPEAK_ENGINE": "command",
        "SPEAK_COMMAND": "true",  # a no-op binary: silent, and hermetic like test_server.py
        "NEMO_AUTOSTART": "0",
        "SPEAK_STATE_DIR": state_dir,
        "SPEAK_CORE_STATE_DIR": state_dir,
        "SPEAK_TELEMETRY": "local",
        # Collecting needs DO_NOT_TRACK cleared, which lets the install ping run;
        # debug turns it, and any shipping, into a log line instead of a request.
        "DO_NOT_TRACK": "",
        "SPEAK_TELEMETRY_DEBUG": "1",
        "HOME": home,
    }


@asynccontextmanager
async def connected(state_dir: str, home: str, client_info: mcp_types.Implementation | None = None):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "speak"],
        env=_env(state_dir, home),
        cwd=str(Path.cwd()),
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, client_info=client_info) as session,
    ):
        await session.initialize()
        yield session


def _telemetry_db(home: Path) -> Path:
    return home / "Library" / "Application Support" / "speak" / "telemetry.db"


async def test_speaking_through_the_real_server_writes_a_local_row(tmp_path):
    """Proof that `build()` really does hand the queue's outcomes to a
    `TelemetrySession`, and that session really does write to the configured
    config directory - not just that the pieces work in isolation. The MCP SDK's
    default client_info name is "mcp", which `normalize_harness` correctly calls
    "other" rather than "unknown" (it was declared, just not one of the three
    named harnesses) or a wrong guess."""
    home = tmp_path / "home"
    home.mkdir()

    with tempfile.TemporaryDirectory() as state_dir:
        async with connected(state_dir, str(home)) as session:
            await session.call_tool("speak", {"text": "hello", "intent": "task_done"})
            await _wait_for_a_row(home)

    conn = store.connect(_telemetry_db(home), SCHEMA)
    try:
        rows = store.unshipped_rows(conn, SCHEMA.rows)
        assert len(rows) == 1
        assert rows[0]["direction"] == "out"
        assert rows[0]["source"] == "speak"
        assert rows[0]["intent"] == "task_done"
        assert rows[0]["engine"] == "command"
        assert rows[0]["status"] == "ok"

        sessions = store.sessions_by_id(conn, [rows[0]["session_id"]])
        assert sessions[0]["harness"] == "other"
    finally:
        conn.close()


async def test_the_clients_own_client_info_overrides_the_environment_fallback(tmp_path):
    """spec 2, tier 1: the `sessions` row opens with the environment's guess
    (unknown here - `CLAUDE_PLUGIN_ROOT` is not set) and is corrected on the first
    tool call once the MCP client's own `client_info` is known."""
    home = tmp_path / "home"
    home.mkdir()

    with tempfile.TemporaryDirectory() as state_dir:
        async with connected(
            state_dir,
            str(home),
            client_info=mcp_types.Implementation(name="Gemini CLI", version="1.2.3"),
        ) as session:
            await session.call_tool("speak", {"text": "hello"})
            await _wait_for_a_row(home)

    conn = store.connect(_telemetry_db(home), SCHEMA)
    try:
        rows = store.unshipped_rows(conn, SCHEMA.rows)
        sessions = store.sessions_by_id(conn, [rows[0]["session_id"]])
        assert sessions[0]["harness"] == "gemini-cli"
        assert sessions[0]["harness_version"] == "1.2.3"
    finally:
        conn.close()


async def test_transcribe_also_corrects_the_harness_from_client_info(tmp_path):
    """`speak` proves the wiring above; `transcribe` calls `note_harness` too, and
    nothing else covered it - a session whose only tool use was transcription must
    not be left with the environment's guessed harness. A missing file is enough:
    `note_harness` runs before `transcribe` even looks at the path."""
    home = tmp_path / "home"
    home.mkdir()

    with tempfile.TemporaryDirectory() as state_dir:
        async with connected(
            state_dir,
            str(home),
            client_info=mcp_types.Implementation(name="Gemini CLI", version="1.2.3"),
        ) as session:
            await session.call_tool("transcribe", {"path": str(tmp_path / "missing.wav")})

    conn = store.connect(_telemetry_db(home), SCHEMA)
    try:
        row = conn.execute("SELECT harness, harness_version FROM sessions").fetchone()
        assert row["harness"] == "gemini-cli"
        assert row["harness_version"] == "1.2.3"
    finally:
        conn.close()


async def test_transcription_status_also_corrects_the_harness_from_client_info(tmp_path):
    """Same proof for the other transcription tool, exercised on its own so neither
    depends on the other having run first."""
    home = tmp_path / "home"
    home.mkdir()

    with tempfile.TemporaryDirectory() as state_dir:
        async with connected(
            state_dir,
            str(home),
            client_info=mcp_types.Implementation(name="Gemini CLI", version="1.2.3"),
        ) as session:
            await session.call_tool("transcription_status", {})

    conn = store.connect(_telemetry_db(home), SCHEMA)
    try:
        row = conn.execute("SELECT harness, harness_version FROM sessions").fetchone()
        assert row["harness"] == "gemini-cli"
        assert row["harness_version"] == "1.2.3"
    finally:
        conn.close()


async def _wait_for_a_row(home: Path, timeout: float = 10.0) -> None:
    db_path = _telemetry_db(home)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if db_path.exists():
            conn = store.connect(db_path, SCHEMA)
            try:
                if store.unshipped_rows(conn, SCHEMA.rows):
                    return
            finally:
                conn.close()
        await asyncio.sleep(0.05)
    raise AssertionError(f"no telemetry row appeared under {db_path} in time")
