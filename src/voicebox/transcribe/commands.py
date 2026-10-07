"""Running the external programs transcription depends on."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

FFMPEG_REMEDY = "install ffmpeg (on macOS: brew install ffmpeg)"

# ffprobe reads only a file's container and stream headers, never the media itself,
# so even a multi-hour recording on a slow disk answers in well under this. Past it,
# the read has stalled - a network mount that dropped, say - rather than merely
# being slow, and `probe()` needs to say so instead of hanging the tool call.
PROBE_TIMEOUT_S = 30.0

# ffmpeg conversion re-encodes the whole file and runs in the worker, off the
# tool's own critical path, so a long recording legitimately taking minutes must
# not trip this - it is generous on purpose, and only a process that is genuinely
# stuck should ever hit it.
CONVERT_TIMEOUT_S = 30 * 60.0


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
    timed_out: bool = False


Runner = Callable[[Sequence[str]], Awaitable[CommandResult]]


async def run_command(argv: Sequence[str], *, timeout: float) -> CommandResult:
    """Run one command to completion, or kill it once `timeout` passes.

    `FileNotFoundError` means it is not installed - unrelated to timing, so it is
    left to propagate before the child ever starts. A child that starts but does
    not answer within `timeout` is killed rather than abandoned, so a stalled
    ffprobe or ffmpeg does not keep running after the call that started it has
    given up on it. The timeout itself comes back as an ordinary non-zero result
    whose stderr names it, so `probe()` and `convert()` turn it into
    `ProbeFailed`/`ConversionFailed` exactly the way they turn any other failure -
    nothing here has to know about either of those types.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        message = f"{argv[0]} timed out after {timeout:.0f}s"
        return CommandResult(-1, b"", message.encode(), timed_out=True)
    returncode = process.returncode if process.returncode is not None else -1
    return CommandResult(returncode, stdout, stderr)


async def run_probe_command(argv: Sequence[str]) -> CommandResult:
    """`run_command` bound to the ffprobe ceiling - the default `Runner` for `probe()`."""
    return await run_command(argv, timeout=PROBE_TIMEOUT_S)


async def run_convert_command(argv: Sequence[str]) -> CommandResult:
    """`run_command` bound to the ffmpeg-conversion ceiling - the default `Runner`
    for `convert()`, threaded through the worker as its own default too."""
    return await run_command(argv, timeout=CONVERT_TIMEOUT_S)
