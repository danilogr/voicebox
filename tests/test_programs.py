import sys

from voicebox import programs as commands
from voicebox.programs import CommandResult, run_command


async def test_a_command_that_finishes_within_its_timeout_returns_normally():
    result = await run_command([sys.executable, "-c", "print('hi')"], timeout=5.0)
    assert result.returncode == 0
    assert result.stdout.strip() == b"hi"


async def test_a_command_past_its_timeout_is_killed_and_named_in_the_result():
    """The point of the timeout: a stalled child must not hang the caller, and the
    failure it reports has to say why rather than looking like an ordinary crash."""
    result = await run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
    assert result.returncode != 0
    assert "timed out after" in result.stderr.decode()


async def test_run_probe_command_uses_the_probe_ceiling(monkeypatch):
    seen = {}

    async def fake(argv, *, timeout: float) -> CommandResult:
        seen["timeout"] = timeout
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(commands, "run_command", fake)
    await commands.run_probe_command(["ffprobe"])
    assert seen["timeout"] == commands.PROBE_TIMEOUT_S


async def test_run_convert_command_uses_the_conversion_ceiling(monkeypatch):
    seen = {}

    async def fake(argv, *, timeout: float) -> CommandResult:
        seen["timeout"] = timeout
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(commands, "run_command", fake)
    await commands.run_convert_command(["ffmpeg"])
    assert seen["timeout"] == commands.CONVERT_TIMEOUT_S


async def test_a_command_that_outlives_its_timeout_says_it_timed_out():
    result = await run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert result.timed_out is True and result.returncode == -1
