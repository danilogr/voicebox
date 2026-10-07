"""Encoding the joined WAV: ffmpeg's arguments per format, and its failures named."""

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from voicebox.programs import CommandResult, ConversionFailed, ToolMissing
from voicebox.synthesize.encode import FORMATS, encode, encode_argv


def test_the_formats_are_the_contracts():
    assert FORMATS == ("wav", "m4a", "mp3")


@pytest.mark.parametrize(
    ("fmt", "codec"),
    [("m4a", ["-c:a", "aac", "-b:a", "128k"]), ("mp3", ["-c:a", "libmp3lame", "-q:a", "2"])],
)
def test_each_format_has_its_codec(fmt, codec):
    argv = encode_argv(Path("/w/in.wav"), Path(f"/w/out.{fmt}"), fmt)
    assert argv[:7] == ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", "/w/in.wav"]
    assert argv[7:] == ["-vn", *codec, f"/w/out.{fmt}"]


async def test_a_missing_ffmpeg_is_named_with_its_remedy(tmp_path):
    async def missing(argv) -> CommandResult:
        raise FileNotFoundError(argv[0])

    with pytest.raises(ToolMissing, match="ffmpeg is not installed"):
        await encode(tmp_path / "in.wav", tmp_path / "out.mp3", "mp3", run=missing)


async def test_a_failing_ffmpeg_says_why_and_whether_it_timed_out(tmp_path):
    async def fails(argv) -> CommandResult:
        return CommandResult(1, b"", b"Unknown encoder 'libmp3lame'")

    async def stalls(argv) -> CommandResult:
        return CommandResult(-1, b"", b"ffmpeg timed out after 1800s", timed_out=True)

    with pytest.raises(ConversionFailed, match="libmp3lame") as failed:
        await encode(tmp_path / "in.wav", tmp_path / "out.mp3", "mp3", run=fails)
    assert failed.value.timed_out is False
    with pytest.raises(ConversionFailed) as stalled:
        await encode(tmp_path / "in.wav", tmp_path / "out.m4a", "m4a", run=stalls)
    assert stalled.value.timed_out is True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("fmt", ["m4a", "mp3"])
async def test_this_machines_ffmpeg_writes_each_format(tmp_path, fmt):
    source = tmp_path / "in.wav"
    with wave.open(str(source), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x00\x01" * 22050)
    await encode(source, tmp_path / f"out.{fmt}", fmt)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(tmp_path / f"out.{fmt}"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout) - 1.0) < 0.2
