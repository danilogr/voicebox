"""What a media file holds, read with ffprobe before any work is committed to it."""

import json
from dataclasses import dataclass
from pathlib import Path

from speak.transcribe.commands import FFMPEG_REMEDY, Runner, ToolMissing, run_command

PROBE_ARGS = (
    "-v",
    "error",
    "-show_entries",
    "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels",
    "-of",
    "json",
)


class ProbeFailed(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """ffprobe ran but could not read the file."""


@dataclass(frozen=True, slots=True)
class Probe:
    duration_s: float
    size_bytes: int
    container: str | None
    codec: str | None
    sample_rate: int | None
    channels: int | None

    @property
    def has_audio(self) -> bool:
        return self.codec is not None


def parse_probe(payload: bytes, size_bytes: int) -> Probe:
    data = json.loads(payload)
    fmt = data.get("format") or {}
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    return Probe(
        duration_s=_float(fmt.get("duration")),
        size_bytes=size_bytes,
        container=fmt.get("format_name"),
        codec=audio.get("codec_name") if audio else None,
        sample_rate=_int(audio.get("sample_rate")) if audio else None,
        channels=_int(audio.get("channels")) if audio else None,
    )


async def probe(path: Path, *, run: Runner = run_command, ffprobe: str = "ffprobe") -> Probe:
    try:
        result = await run([ffprobe, *PROBE_ARGS, str(path)])
    except FileNotFoundError:
        raise ToolMissing("ffprobe", FFMPEG_REMEDY) from None
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip()
        raise ProbeFailed(reason or "ffprobe could not read the file")
    return parse_probe(result.stdout, path.stat().st_size)


def _float(value: object) -> float:
    if not isinstance(value, int | float | str):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _int(value: object) -> int | None:
    if not isinstance(value, int | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
