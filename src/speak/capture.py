"""Microphone capture: audio in, WAV bytes out.

The decision about when an utterance has ended is a pure state machine fed block
energies, so every path through it is a table test with no audio hardware. The
`sounddevice` stream is a thin shell around it.
"""

import array
import asyncio
import contextlib
import io
import logging
import math
import wave
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Any, Protocol

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


SAMPLE_RATE = 16_000
BLOCK_MS = 30.0
FULL_SCALE = 32768.0


class RecorderUnavailable(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """No usable microphone: the binding is missing, or the OS refused access."""


@dataclass(frozen=True, slots=True)
class Recording:
    """One captured utterance and the reason capture stopped."""

    wav: bytes
    reason: State

    @property
    def has_speech(self) -> bool:
        return self.reason is not State.SILENT


class Recorder(Protocol):
    async def record(self, settings: TrackerSettings) -> Recording: ...


def rms(block: bytes) -> float:
    """Root-mean-square level of a block of little-endian 16-bit mono samples."""
    samples = array.array("h")
    samples.frombytes(block[: len(block) - len(block) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / FULL_SCALE


def to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw mono 16-bit samples in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def drive(read: Callable[[], bytes], settings: TrackerSettings) -> tuple[bytes, State]:
    """Pull blocks from `read` until the tracker says to stop.

    Everything but `read` is deterministic, which is what makes capture testable
    without a microphone.
    """
    tracker = SilenceTracker(settings)
    blocks: list[bytes] = []
    while True:
        block = read()
        blocks.append(block)
        if tracker.feed(rms(block)) in FINAL:
            return b"".join(blocks), tracker.state


class MicrophoneRecorder:
    """Records the default input device until the tracker finishes."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        block_ms: float = BLOCK_MS,
        device: str | int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._block_frames = int(sample_rate * block_ms / 1000)
        self._device = device

    async def record(self, settings: TrackerSettings) -> Recording:
        """Record one utterance, off the event loop so nothing else stalls."""
        return await asyncio.to_thread(self._record, settings)

    def _record(self, settings: TrackerSettings) -> Recording:
        try:
            import sounddevice
        except (ImportError, OSError) as error:
            raise RecorderUnavailable(
                f"no microphone binding available: {error}. Run uv sync in the plugin directory."
            ) from error

        # Constructing the stream already claims the device, and sounddevice streams
        # have no __del__ — so from here on every path out must close it.
        stream = self._open(sounddevice)
        with contextlib.closing(stream):
            try:
                stream.start()
            except sounddevice.PortAudioError as error:
                raise RecorderUnavailable(_microphone_hint(error)) from error
            try:
                pcm, reason = drive(lambda: bytes(stream.read(self._block_frames)[0]), settings)
            except sounddevice.PortAudioError as error:
                raise RecorderUnavailable(
                    f"the microphone failed during recording: {error}. "
                    "Check that the input device is still connected."
                ) from error
        return Recording(wav=to_wav(pcm, self._sample_rate), reason=reason)

    def _open(self, sounddevice: ModuleType) -> Any:
        try:
            return sounddevice.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=self._block_frames,
                dtype="int16",
                channels=1,
                device=self._device,
            )
        except Exception as error:  # PortAudioError, or ValueError for a bad device
            raise RecorderUnavailable(_microphone_hint(error)) from error


def _microphone_hint(error: BaseException) -> str:
    return (
        f"could not open the microphone: {error}. "
        "On macOS, grant microphone access to the app running Claude Code in "
        "System Settings > Privacy & Security > Microphone."
    )
