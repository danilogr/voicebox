"""Turning whatever the file is into what the model reads."""

from pathlib import Path

from speak.transcribe.commands import FFMPEG_REMEDY, Runner, ToolMissing, run_convert_command
from speak.transcribe.probe import Probe

CONVERT_ARGS = ("-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le")


class ConversionFailed(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """ffmpeg ran but could not produce audio the model can read."""


def needs_conversion(found: Probe) -> bool:
    """Pass through only 16-bit PCM in a WAV container.

    A looser rule - convert only what is not named `.wav` - passes a WAV whose
    samples the model cannot use and returns an empty transcript with no error.
    Converting a file that was already correct costs a second.
    """
    containers = set((found.container or "").split(","))
    return not ("wav" in containers and found.codec == "pcm_s16le")


async def convert(
    source: Path, dest: Path, *, run: Runner = run_convert_command, ffmpeg: str = "ffmpeg"
) -> None:
    argv = [
        ffmpeg,
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        *CONVERT_ARGS,
        str(dest),
    ]
    try:
        result = await run(argv)
    except FileNotFoundError:
        raise ToolMissing("ffmpeg", FFMPEG_REMEDY) from None
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip()
        raise ConversionFailed(reason or "ffmpeg could not convert the file")
