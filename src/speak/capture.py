"""Microphone capture: audio in, WAV bytes out.

The decision about when an utterance has ended is a pure state machine fed block
energies, so every path through it is a table test with no audio hardware. The
`sounddevice` stream is a thin shell around it.
"""

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class State(Enum):
    """Where a recording is: the first three continue, the last three stop."""

    CALIBRATING = "calibrating"
    WAITING = "waiting"
    SPEAKING = "speaking"
    DONE = "done"
    SILENT = "silent"
    CAPPED = "capped"


FINAL = frozenset({State.DONE, State.SILENT, State.CAPPED})


@dataclass(frozen=True, slots=True)
class TrackerSettings:
    """Limits for one recording, in milliseconds."""

    block_ms: float = 30.0
    calibrate_ms: float = 300.0
    silence_ms: float = 1500.0
    start_timeout_ms: float = 10_000.0
    max_ms: float = 60_000.0
    noise_multiple: float = 3.0
    floor: float = 0.01


class SilenceTracker:
    """Decides when to stop recording, one block of audio energy at a time.

    A fixed threshold fails on a quiet microphone and in a noisy room alike, so
    the gate is calibrated from the ambient level at the start of the recording
    and clamped to a floor.
    """

    def __init__(self, settings: TrackerSettings) -> None:
        self._settings = settings
        self._elapsed_ms = 0.0
        self._ambient: list[float] = []
        self._threshold = settings.floor
        self._silence_ms = 0.0
        self._state = State.CALIBRATING

    @property
    def state(self) -> State:
        return self._state

    @property
    def threshold(self) -> float:
        return self._threshold

    def feed(self, energy: float) -> State:
        """Take one block's energy and report whether recording continues."""
        if self._state in FINAL:
            return self._state

        self._elapsed_ms += self._settings.block_ms
        if self._elapsed_ms >= self._settings.max_ms:
            self._state = State.CAPPED
            return self._state

        if self._state is State.CALIBRATING:
            return self._calibrate(energy)
        if self._state is State.WAITING:
            return self._wait(energy)
        return self._speak(energy)

    def _calibrate(self, energy: float) -> State:
        self._ambient.append(energy)
        if self._elapsed_ms >= self._settings.calibrate_ms:
            ambient = sum(self._ambient) / len(self._ambient)
            self._threshold = max(self._settings.floor, ambient * self._settings.noise_multiple)
            self._state = State.WAITING
            logger.debug("microphone gate set to %.4f", self._threshold)
        return self._state

    def _wait(self, energy: float) -> State:
        if energy >= self._threshold:
            self._state = State.SPEAKING
            self._silence_ms = 0.0
        elif self._elapsed_ms >= self._settings.start_timeout_ms:
            self._state = State.SILENT
        return self._state

    def _speak(self, energy: float) -> State:
        if energy >= self._threshold:
            self._silence_ms = 0.0
            return self._state
        self._silence_ms += self._settings.block_ms
        if self._silence_ms >= self._settings.silence_ms:
            self._state = State.DONE
        return self._state
