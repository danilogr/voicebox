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
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class State(Enum):
    """Where a recording is: the first three continue, the rest stop."""

    CALIBRATING = "calibrating"
    WAITING = "waiting"
    SPEAKING = "speaking"
    DONE = "done"
    SILENT = "silent"
    CAPPED = "capped"
    CANCELLED = "cancelled"


FINAL = frozenset({State.DONE, State.SILENT, State.CAPPED, State.CANCELLED})


@dataclass(frozen=True, slots=True)
class TrackerSettings:
    """Limits for one recording, in milliseconds."""

    block_ms: float = 30.0
    calibrate_ms: float = 300.0
    silence_ms: float = 3000.0
    start_timeout_ms: float = 10_000.0
    max_ms: float = 240_000.0
    noise_multiple: float = 3.0
    floor: float = 0.01
    ceiling: float = 0.05


class SilenceTracker:
    """Decides when to stop recording, one block of audio energy at a time.

    A fixed threshold fails on a quiet microphone and in a noisy room alike, so
    the gate is calibrated from the ambient level at the start of the recording
    and clamped between a floor and a ceiling.
    """

    def __init__(self, settings: TrackerSettings) -> None:
        self._settings = settings
        self._elapsed_ms = 0.0
        self._ambient: list[float] = []
        self._threshold = settings.floor
        self._silence_ms = 0.0
        self._state = State.CALIBRATING
        self._heard_speech = False

    @property
    def state(self) -> State:
        return self._state

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def heard_speech(self) -> bool:
        """True once the gate has opened, whatever the recording ends as.

        The terminal state cannot answer this. `feed` checks the cap before the
        start timeout, so whenever the cap is the shorter of the two a recording
        of an empty room ends CAPPED rather than SILENT.
        """
        return self._heard_speech

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
            gate = ambient * self._settings.noise_multiple
            # Clamped from above as well as below. Talking immediately is what a
            # user does when nothing cues them to wait, and then calibration
            # averages their own voice: an unclamped gate lands near three times
            # speech level, which no microphone reaches, so `_wait` never fires and
            # the recording ends SILENT at the start timeout. A gate that is too low
            # costs a longer recording; one that is too high loses the utterance.
            self._threshold = min(max(gate, self._settings.floor), self._settings.ceiling)
            self._state = State.WAITING
            logger.debug("microphone gate set to %.4f", self._threshold)
        return self._state

    def _wait(self, energy: float) -> State:
        if energy >= self._threshold:
            self._state = State.SPEAKING
            self._heard_speech = True
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
    """One captured utterance, why capture stopped, and whether anyone spoke.

    `has_speech` is carried rather than inferred from `reason`, because CAPPED
    means only that the recording ran its length: an empty room reaches it
    whenever the cap is shorter than the start timeout. Inferring speech from it
    would send room tone to the transcriber, and a transcription model given
    silence reliably emits a stock phrase, which the caller would then treat as
    the user's own words.
    """

    wav: bytes
    reason: State
    has_speech: bool


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


def drive(
    read: Callable[[], bytes],
    settings: TrackerSettings,
    cancel: threading.Event | None = None,
) -> tuple[bytes, State, bool]:
    """Pull blocks from `read` until the tracker says to stop, or `cancel` is set.

    Returns the audio, why recording stopped, and whether the gate ever opened.
    Everything but `read` is deterministic, which is what makes capture testable
    without a microphone. `cancel` is how a caller that has stopped waiting tells
    this loop to let the device go; it is checked once per block, so the loop ends
    within one block of being told rather than running the cap out.
    """
    tracker = SilenceTracker(settings)
    blocks: list[bytes] = []
    while True:
        if cancel is not None and cancel.is_set():
            return b"".join(blocks), State.CANCELLED, tracker.heard_speech
        block = read()
        blocks.append(block)
        if tracker.feed(rms(block)) in FINAL:
            return b"".join(blocks), tracker.state, tracker.heard_speech


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
        """Record one utterance, off the event loop so nothing else stalls.

        Cancelling this await returns at once, but the worker thread would keep the
        device open until the cap ran out. A `listen` straight after a cancelled one
        would then pass both locks and open a second stream against a device the
        first thread still holds, and interpreter shutdown, which joins non-daemon
        threads, would hold the session shut for the rest of the recording. So the
        thread is told as well, and leaves within one block.
        """
        cancel = threading.Event()
        try:
            return await asyncio.to_thread(self._record, settings, cancel)
        finally:
            cancel.set()

    def _record(self, settings: TrackerSettings, cancel: threading.Event) -> Recording:
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
                pcm, reason, heard_speech = drive(
                    lambda: bytes(stream.read(self._block_frames)[0]), settings, cancel
                )
            except sounddevice.PortAudioError as error:
                raise RecorderUnavailable(
                    f"the microphone failed during recording: {error}. "
                    "Check that the input device is still connected."
                ) from error
        return Recording(wav=to_wav(pcm, self._sample_rate), reason=reason, has_speech=heard_speech)

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
