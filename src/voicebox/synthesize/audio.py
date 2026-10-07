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


class OutputTooLargeError(ValueError):
    """The joined audio is larger than a WAV file can hold."""


# The wave module packs 36 + the data size into 32 bits.
MAX_DATA_BYTES = 2**32 - 37


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

    Every header is read, and the total size checked, before `destination` is created.

    Raises:
        FormatMismatchError: a piece's channels, width, rate or compression differ.
        OutputTooLargeError: the joined audio would not fit a WAV file's 4 GiB limit.
        ValueError: there is nothing to join.
        wave.Error, EOFError: a piece is not a WAV file.
    """
    if not pieces:
        raise ValueError("no audio to join")
    counts: list[int] = []
    first: Shape | None = None
    for index, piece in enumerate(pieces):
        with wave.open(str(piece.path), "rb") as source:
            shape = _shape(source)
            first = first or shape
            if shape != first:
                raise FormatMismatchError(
                    f"chunk {index + 1} is {_describe(shape)}; the first was {_describe(first)}"
                )
            counts.append(source.getnframes())
    assert first is not None
    channels, width, rate, _ = first
    silences = [
        rate * (paragraph_gap_ms if piece.paragraph_end else gap_ms) // 1000
        for piece in pieces[:-1]
    ]
    frames = sum(counts) + sum(silences)
    if frames * width * channels > MAX_DATA_BYTES:
        raise OutputTooLargeError(
            f"{frames * width * channels} bytes of audio do not fit one WAV file "
            f"(at most {MAX_DATA_BYTES})"
        )
    with wave.open(str(destination), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        for index, piece in enumerate(pieces):
            with wave.open(str(piece.path), "rb") as source:
                out.writeframes(source.readframes(counts[index]))
            if index < len(silences):
                out.writeframes(_silence(silences[index], width, channels))
    return round(frames / rate * 1000)
