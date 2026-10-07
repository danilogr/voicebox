"""End-to-end: a real server process driven over stdio by a real MCP client.

`VOICEBOX_ENGINE=command` points at a recorder script, so the suite exercises the
whole path without producing audio.
"""

import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from voicebox.__main__ import build_supervisor
from voicebox.config import Settings

RECORDER = """#!{python}
import json, sys, pathlib
log = pathlib.Path({log!r})
spoken = log.read_text().splitlines() if log.exists() else []
spoken.append(json.dumps(sys.argv[1:]))
log.write_text("\\n".join(spoken) + "\\n")
"""


@pytest.fixture
def recorder(tmp_path):
    log = tmp_path / "spoken.log"
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER.format(python=sys.executable, log=str(log)))
    script.chmod(0o755)
    return script, log


def server_env(command: str, state_dir: str, extra_env: dict[str, str] | None = None):
    """The environment one subprocess server runs in.

    The developer's shell is inherited, so everything that decides whether a session
    manages the shared speech server is pinned here. Every session may start one, so
    `NEMO_AUTOSTART=0` is the whole guard: without it each of these subprocesses would
    register in the real state directory and, on teardown, SIGTERM the pid in the real
    `server.json`, stopping the speech server the developer's other Claude Code windows
    are using.
    """
    return {
        **os.environ,
        "VOICEBOX_ENGINE": "command",
        "VOICEBOX_COMMAND": command,
        "NEMO_AUTOSTART": "0",
        "VOICEBOX_STATE_DIR": state_dir,
        "SPEAK_CORE_STATE_DIR": state_dir,
        # Telemetry is off in every subprocess here.
        "VOICEBOX_TELEMETRY": "off",
        "DO_NOT_TRACK": "1",
        **(extra_env or {}),
    }


@asynccontextmanager
async def connected(command: str, extra_env: dict[str, str] | None = None):
    with tempfile.TemporaryDirectory() as state_dir:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "voicebox"],
            env=server_env(command, state_dir, extra_env),
            cwd=str(Path.cwd()),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as sess:
            await sess.initialize()
            yield sess


def test_the_suite_cannot_reach_the_machine_wide_speech_server(monkeypatch, tmp_path):
    """Every subprocess this file starts is hermetic, whatever the shell exports."""
    monkeypatch.delenv("NEMO_AUTOSTART", raising=False)

    settings = Settings.from_env(server_env("true", str(tmp_path)))

    assert build_supervisor(settings) is None
    assert settings.state_dir == tmp_path
    assert settings.server.shared_dir == tmp_path


async def text_of(session, name, **arguments: object) -> str:
    result = await session.call_tool(name, arguments)
    return result.content[0].text


async def test_initializes_and_reports_its_name(recorder):
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        result = await session.list_tools()
        assert {tool.name for tool in result.tools} == {
            "read_aloud",
            "stop_reading",
            "reading_status",
            "transcribe",
            "synthesize",
            "job_status",
        }


async def test_read_aloud_reaches_the_engine_and_says_the_normalized_text(recorder):
    script, log = recorder
    async with connected(f"{sys.executable} {script}") as session:
        assert await text_of(session, "read_aloud", text="  hello   there  ") == ("reading")
        await _wait_for_lines(log, 1)

    assert json.loads(log.read_text().splitlines()[0]) == ["hello there"]


async def test_utterances_reach_the_engine_in_order(recorder):
    script, log = recorder
    async with connected(f"{sys.executable} {script}") as session:
        for word in ("one", "two", "three"):
            await text_of(session, "read_aloud", text=word)
        await _wait_for_lines(log, 3)

    spoken = [json.loads(line)[0] for line in log.read_text().splitlines()]
    assert spoken == ["one", "two", "three"]


async def test_an_empty_sentence_comes_back_as_an_error(recorder):
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        result = await session.call_tool("read_aloud", {"text": "   "})
        assert result.is_error is True
        assert "nothing to speak" in result.content[0].text


async def test_a_sentence_with_a_nul_is_refused_and_reading_goes_on(recorder):
    """A NUL handed to the player would end the queue's worker: every later sentence
    would answer "reading" and never be heard."""
    script, log = recorder
    async with connected(f"{sys.executable} {script}") as session:
        refused = await session.call_tool("read_aloud", {"text": "bad\x00text"})
        assert refused.is_error is True
        assert await text_of(session, "read_aloud", text="A normal sentence.") == "reading"
        await _wait_for_lines(log, 1)
        assert "nothing left to read" in await text_of(session, "reading_status")

    assert json.loads(log.read_text().splitlines()[0]) == ["A normal sentence."]


async def test_stop_reading_answers_even_when_nothing_is_playing(recorder):
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        assert "stopped" in await text_of(session, "stop_reading")


async def test_reading_status_is_offered_over_stdio(recorder):
    """The tool exists for any MCP client, so it is checked the way they receive it."""
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        assert "reading_status" in {tool.name for tool in (await session.list_tools()).tools}
        assert "nothing left to read" in await text_of(session, "reading_status")


async def test_the_character_cap_is_honoured_end_to_end(recorder):
    script, log = recorder
    async with connected(f"{sys.executable} {script}", {"VOICEBOX_MAX_CHARS": "11"}) as session:
        await text_of(session, "read_aloud", text="one two three four")
        await _wait_for_lines(log, 1)

    assert json.loads(log.read_text().splitlines()[0]) == ["one two…"]


async def test_nothing_is_written_to_stdout_except_protocol_frames(recorder):
    """A stray print would desync the session; a completed round trip proves it did not."""
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        for _ in range(3):
            await text_of(session, "read_aloud", text="hi")
        assert await text_of(session, "stop_reading") is not None


async def test_a_client_sees_the_narrate_prompt_over_stdio(recorder):
    script, _ = recorder
    async with connected(f"{sys.executable} {script}") as session:
        prompts = {p.name for p in (await session.list_prompts()).prompts}
        assert prompts == {"narrate"}


async def _wait_for_lines(log: Path, count: int, timeout: float = 5.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if log.exists() and len(log.read_text().splitlines()) >= count:
            return
        await asyncio.sleep(0.02)
    found = log.read_text() if log.exists() else "nothing"
    raise AssertionError(f"expected {count} utterances, log has {found}")


async def test_closing_the_session_kills_a_playing_utterance(tmp_path):
    """A session that ends mid-sentence must not leave the machine talking."""
    import asyncio
    import signal

    marker = tmp_path / "pid"
    player = tmp_path / "player.py"
    player.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    player.chmod(0o755)

    async with connected(f"{sys.executable} {player}") as session:
        await text_of(session, "read_aloud", text="a long sentence")
        for _ in range(250):
            if marker.exists():
                break
            await asyncio.sleep(0.02)
        assert marker.exists(), "player never started"
        child = int(marker.read_text())

    for _ in range(250):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    os.kill(child, signal.SIGKILL)
    raise AssertionError(f"player {child} survived the session")
