#!/usr/bin/env python3
"""Synthesise the three cue sounds that ship with the plugin.

Run from the repo root: `uv run python scripts/make-cues.py`

The sounds are generated rather than copied so the plugin carries no one else's
audio assets and works on any platform. Mono 16-bit PCM WAV at 44.1 kHz, which is
the one format every player on every platform reads, Windows SoundPlayer included.
"""

import array
import math
import wave
from pathlib import Path

RATE = 44_100
PEAK = 0.42  # a cue should be audible, not startling

OUT = Path(__file__).resolve().parent.parent / "src" / "speak" / "assets"


def tone(
    freq: float,
    ms: float,
    *,
    decay_ms: float,
    partials: tuple[tuple[float, float], ...] = (),
    attack_ms: float = 3.0,
) -> list[float]:
    """One decaying sine, plus optional partials as (multiple, amplitude)."""
    samples = int(RATE * ms / 1000)
    attack = max(1, int(RATE * attack_ms / 1000))
    out = []
    for n in range(samples):
        t = n / RATE
        value = math.sin(2 * math.pi * freq * t)
        for multiple, amplitude in partials:
            value += amplitude * math.sin(2 * math.pi * freq * multiple * t)
        envelope = math.exp(-t / (decay_ms / 1000))
        if n < attack:  # ramp in, or the onset clicks
            envelope *= n / attack
        out.append(value * envelope)
    return out


def write(name: str, frames: list[float]) -> None:
    peak = max((abs(v) for v in frames), default=1.0) or 1.0
    scale = PEAK * 32767 / peak
    samples = array.array("h", (int(max(-32768, min(32767, v * scale))) for v in frames))
    path = OUT / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    print(f"  {name}  {len(frames) / RATE * 1000:.0f} ms  {path.stat().st_size} bytes")


def silence(ms: float) -> list[float]:
    return [0.0] * int(RATE * ms / 1000)


OUT.mkdir(parents=True, exist_ok=True)
print("writing cues to", OUT)

# Open: bright and very short. It is awaited to completion before the microphone
# opens, so every millisecond here is a millisecond the user waits.
write("cue-open.wav", tone(1320.0, 90, decay_ms=20, partials=((2.0, 0.28),)))

# Close: lower and softer, so it is told apart from the opening cue without being
# looked at. Longer is fine - nothing waits on it.
write("cue-close.wav", tone(466.0, 180, decay_ms=58, partials=((2.0, 0.22), (3.0, 0.08))))

# Empty: two falling tones. Descending reads as "no" across cultures, and the added
# low partial gives it the dull quality of an error tone rather than a chime.
write(
    "cue-empty.wav",
    tone(311.0, 130, decay_ms=70, partials=((2.0, 0.3),))
    + silence(10)
    + tone(196.0, 230, decay_ms=95, partials=((2.0, 0.34), (3.0, 0.12))),
)
