"""Joining chunk WAVs: one file, silences between, and a format mismatch is an error."""

import wave
from pathlib import Path

import pytest

from voicebox.synthesize.audio import FormatMismatchError, Piece, concatenate


def wav(path: Path, frames: int, *, rate: int = 22050, width: int = 2, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\x01" * frames * width * channels)
    return path


def test_pieces_are_joined_in_order_with_short_and_long_pauses(tmp_path):
    pieces = [
        Piece(wav(tmp_path / "0.wav", 2205), paragraph_end=False),
        Piece(wav(tmp_path / "1.wav", 2205), paragraph_end=True),
        Piece(wav(tmp_path / "2.wav", 2205), paragraph_end=True),
    ]
    out = tmp_path / "joined.wav"
    duration = concatenate(pieces, out, gap_ms=100, paragraph_gap_ms=500)
    # three 100 ms pieces, a 100 ms gap after the first, a 500 ms gap after the second
    assert duration == 900
    with wave.open(str(out)) as joined:
        shape = (joined.getnchannels(), joined.getsampwidth(), joined.getframerate())
        assert shape == (1, 2, 22050)
        assert joined.getnframes() == 22050 * 900 // 1000
        frames = joined.readframes(joined.getnframes())
    assert frames[: 2205 * 2] == b"\x01" * 2205 * 2
    assert frames[2205 * 2 : 2205 * 2 + 2205 * 2] == b"\x00" * 2205 * 2


def test_no_pause_follows_the_last_piece(tmp_path):
    out = tmp_path / "joined.wav"
    only = [Piece(wav(tmp_path / "0.wav", 22050), True)]
    assert concatenate(only, out, gap_ms=100, paragraph_gap_ms=500) == 1000


def test_eight_bit_silence_is_the_midpoint(tmp_path):
    pieces = [Piece(wav(tmp_path / f"{i}.wav", 10, rate=1000, width=1), False) for i in range(2)]
    out = tmp_path / "joined.wav"
    concatenate(pieces, out, gap_ms=10, paragraph_gap_ms=10)
    with wave.open(str(out)) as joined:
        frames = joined.readframes(joined.getnframes())
    assert frames[10:20] == b"\x80" * 10


@pytest.mark.parametrize("other", [{"rate": 44100}, {"width": 1}, {"channels": 2}])
def test_a_piece_in_another_format_fails_rather_than_corrupting_the_file(tmp_path, other):
    pieces = [
        Piece(wav(tmp_path / "0.wav", 100), False),
        Piece(wav(tmp_path / "1.wav", 100, **other), True),
    ]
    with pytest.raises(FormatMismatchError, match="chunk 2"):
        concatenate(pieces, tmp_path / "joined.wav", gap_ms=100, paragraph_gap_ms=500)


def test_a_piece_that_is_not_a_wav_raises_a_wave_error(tmp_path):
    bad = tmp_path / "0.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises((wave.Error, EOFError)):
        concatenate([Piece(bad, True)], tmp_path / "joined.wav", gap_ms=1, paragraph_gap_ms=1)


def test_nothing_to_join_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no audio"):
        concatenate([], tmp_path / "joined.wav", gap_ms=1, paragraph_gap_ms=1)
