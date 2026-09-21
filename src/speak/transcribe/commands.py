"""Running the external programs transcription depends on."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

FFMPEG_REMEDY = "install ffmpeg (on macOS: brew install ffmpeg)"


class ToolMissing(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """An external program this feature needs is not installed."""

    def __init__(self, program: str, remedy: str) -> None:
        super().__init__(f"{program} is not installed; {remedy}")
        self.program = program


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[Sequence[str]], Awaitable[CommandResult]]


async def run_command(argv: Sequence[str]) -> CommandResult:
    """Run one command to completion. `FileNotFoundError` means it is not installed."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    returncode = process.returncode if process.returncode is not None else -1
    return CommandResult(returncode, stdout, stderr)
