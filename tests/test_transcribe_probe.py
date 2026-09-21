import json
from pathlib import Path
from typing import NoReturn

import pytest

from speak.transcribe.commands import CommandResult, ToolMissing
from speak.transcribe.prepare import ConversionFailed, convert, needs_conversion
from speak.transcribe.probe import ProbeFailed, parse_probe, probe

VIDEO_ONLY_MP4 = {
    "streams": [{"codec_name": "h264", "codec_type": "video"}],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "14.400000"},
}
PCM_WAV = {
    "streams": [
        {
            "codec_name": "pcm_s16le",
            "codec_type": "audio",
            "sample_rate": "22050",
            "channels": 1,
        }
    ],
    "format": {"format_name": "wav", "duration": "5.247710"},
}
FLAC_MKV = {
    "streams": [
        {
            "codec_name": "flac",
            "codec_type": "audio",
            "sample_rate": "16000",
            "channels": 1,
        }
    ],
    "format": {"format_name": "matroska,webm", "duration": "63.144000"},
}


def parsed(payload, size=1000):
    return parse_probe(json.dumps(payload).encode(), size)


def test_a_video_only_file_has_no_audio():
    found = parsed(VIDEO_ONLY_MP4)
    assert not found.has_audio
    assert found.duration_s == pytest.approx(14.4)


def test_a_wav_reports_its_codec_rate_and_channels():
    found = parsed(PCM_WAV, size=231_468)
    assert (found.codec, found.sample_rate, found.channels) == ("pcm_s16le", 22050, 1)
    assert found.size_bytes == 231_468


def test_only_16_bit_pcm_in_a_wav_container_skips_conversion():
    assert not needs_conversion(parsed(PCM_WAV))
    assert needs_conversion(parsed(FLAC_MKV))
    float_wav = {**PCM_WAV, "streams": [{**PCM_WAV["streams"][0], "codec_name": "pcm_f32le"}]}
    assert needs_conversion(parsed(float_wav))


def test_missing_duration_reads_as_zero():
    assert parsed({"streams": [], "format": {}}).duration_s == 0.0


async def test_probe_runs_ffprobe_on_the_file(tmp_path: Path) -> None:
    media = tmp_path / "a.wav"
    media.write_bytes(b"x" * 42)
    seen: list[list[str]] = []

    async def run(argv: list[str]) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(0, json.dumps(PCM_WAV).encode(), b"")

    found = await probe(media, run=run)
    assert seen[0][0] == "ffprobe" and seen[0][-1] == str(media)
    assert found.size_bytes == 42


async def test_probe_names_a_missing_ffprobe(tmp_path: Path) -> None:
    async def run(argv: list[str]) -> NoReturn:
        raise FileNotFoundError(argv[0])

    media = tmp_path / "a.wav"
    media.write_bytes(b"x")
    with pytest.raises(ToolMissing, match="ffprobe is not installed"):
        await probe(media, run=run)


async def test_probe_reports_an_unreadable_file(tmp_path: Path) -> None:
    async def run(argv: list[str]) -> CommandResult:
        return CommandResult(1, b"", b"Invalid data found when processing input")

    media = tmp_path / "a.wav"
    media.write_bytes(b"x")
    with pytest.raises(ProbeFailed, match="Invalid data"):
        await probe(media, run=run)


async def test_convert_writes_16_khz_mono_pcm(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    async def run(argv: list[str]) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(0, b"", b"")

    await convert(Path("in.mkv"), tmp_path / "out.wav", run=run)
    argv = seen[0]
    assert argv[0] == "ffmpeg" and argv[-1] == str(tmp_path / "out.wav")
    assert argv[-7:-1] == ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]


async def test_convert_reports_ffmpeg_failing(tmp_path: Path) -> None:
    async def run(argv: list[str]) -> CommandResult:
        return CommandResult(1, b"", b"Output file is empty")

    with pytest.raises(ConversionFailed, match="Output file is empty"):
        await convert(Path("in.mkv"), tmp_path / "out.wav", run=run)
