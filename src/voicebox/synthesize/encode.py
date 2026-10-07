"""Turning the joined WAV into the format asked for. WAV needs nothing more; m4a and mp3
are ffmpeg's - the same ffmpeg transcription already requires."""

from collections.abc import Sequence
from pathlib import Path

from voicebox.programs import (
    FFMPEG_REMEDY,
    CommandResult,
    ConversionFailed,
    Runner,
    ToolMissing,
    run_command,
)

FORMATS = ("wav", "m4a", "mp3")
CODECS: dict[str, tuple[str, ...]] = {
    "m4a": ("-c:a", "aac", "-b:a", "128k"),
    "mp3": ("-c:a", "libmp3lame", "-q:a", "2"),
}

# Encoding is minutes of audio at most seconds of work each; this only bounds an ffmpeg
# that has stalled, so a long voice-over legitimately taking a while never trips it.
ENCODE_TIMEOUT_S = 30 * 60.0


def encode_argv(source: Path, dest: Path, fmt: str, *, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        *CODECS[fmt],
        str(dest),
    ]


async def run_encode_command(argv: Sequence[str]) -> CommandResult:
    return await run_command(argv, timeout=ENCODE_TIMEOUT_S)


async def encode(source: Path, dest: Path, fmt: str, *, run: Runner = run_encode_command) -> None:
    """Write `dest` in `fmt` from the WAV at `source`.

    Raises:
        ToolMissing: ffmpeg is not installed.
        ConversionFailed: ffmpeg ran and failed, or timed out (`timed_out`).
    """
    try:
        result = await run(encode_argv(source, dest, fmt))
    except FileNotFoundError:
        raise ToolMissing("ffmpeg", FFMPEG_REMEDY) from None
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip()
        raise ConversionFailed(
            reason or f"ffmpeg could not write {fmt}", timed_out=result.timed_out
        )
