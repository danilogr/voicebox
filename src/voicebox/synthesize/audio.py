"""Joining the chunk WAVs into one file, with silence between them.

Every chunk comes from one server and one model, so they share a sample format; one that
does not is a fault, and the job fails rather than writing audio that is corrupt.
"""

import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class FormatMismatchError(ValueError):
    """A chunk's sample format differs from the first chunk's."""


@dataclass(frozen=True, slots=True)
class Piece:
    path: Path
    paragraph_end: bool


def _silence(frames: int, width: int, channels: int) -> bytes:
    # 8-bit WAV is unsigned, so its silence is the midpoint; wider samples are signed.
    sample = b"\x80" if width == 1 else b"\x00" * width
    return sample * channels * frames


Shape = tuple[int, int, int, str]


def _shape(source: wave.Wave_read) -> Shape:
    return (
        source.getnchannels(),
        source.getsampwidth(),
        source.getframerate(),
        source.getcomptype(),
    )


def _describe(shape: Shape) -> str:
    return f"{shape[0]} channel(s), {8 * shape[1]}-bit, {shape[2]} Hz"


def concatenate(
    pieces: Sequence[Piece], destination: Path, *, gap_ms: int, paragraph_gap_ms: int
) -> int:
    """Write `pieces` into `destination` in order and return its length in milliseconds.

    Raises:
        FormatMismatchError: a piece's channels, width, rate or compression differ.
        ValueError: there is nothing to join.
        wave.Error, EOFError: a piece is not a WAV file.
    """
    if not pieces:
        raise ValueError("no audio to join")
    with wave.open(str(pieces[0].path), "rb") as head:
        first = _shape(head)
    channels, width, rate, _ = first
    frames = 0
    with wave.open(str(destination), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        for index, piece in enumerate(pieces):
            with wave.open(str(piece.path), "rb") as source:
                shape = _shape(source)
                if shape != first:
                    raise FormatMismatchError(
                        f"chunk {index + 1} is {_describe(shape)}; the first was {_describe(first)}"
                    )
                count = source.getnframes()
                out.writeframes(source.readframes(count))
                frames += count
            if index < len(pieces) - 1:
                pause = paragraph_gap_ms if piece.paragraph_end else gap_ms
                silent = rate * pause // 1000
                out.writeframes(_silence(silent, width, channels))
                frames += silent
    return round(frames / rate * 1000)
