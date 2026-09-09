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
from collections.abc import Callable, Sequence
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
    DEAF = "deaf"


FINAL = frozenset({State.DONE, State.SILENT, State.CAPPED, State.CANCELLED, State.DEAF})


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
        self._peak = 0.0

    @property
    def state(self) -> State:
        return self._state

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def peak(self) -> float:
        """The loudest block seen, so a failure can say how close it came."""
        return self._peak

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

        self._peak = max(self._peak, energy)
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
            # Exact zeros, every sample, are not a quiet room: a live microphone
            # always carries a noise floor, so digital silence means the OS handed
            # this process a muted stream. Treating it as a quiet room is what makes
            # a dead default device cost the whole start timeout and then report
            # "heard nothing", whose remedies are all about speaking louder.
            if not any(self._ambient):
                self._state = State.DEAF
                return self._state
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
    device: str | int | None = None
    threshold: float = 0.0
    peak: float = 0.0


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


@dataclass(frozen=True, slots=True)
class Capture:
    """One pass over one device: the audio, why it stopped, and what the gate saw."""

    pcm: bytes
    reason: State
    has_speech: bool
    threshold: float
    peak: float


def drive(
    read: Callable[[], bytes],
    settings: TrackerSettings,
    cancel: threading.Event | None = None,
) -> Capture:
    """Pull blocks from `read` until the tracker says to stop, or `cancel` is set.

    Returns the audio, why recording stopped, and whether the gate ever opened.
    Everything but `read` is deterministic, which is what makes capture testable
    without a microphone. `cancel` is how a caller that has stopped waiting tells
    this loop to let the device go; it is checked once per block, so the loop ends
    within one block of being told rather than running the cap out.
    """
    tracker = SilenceTracker(settings)
    blocks: list[bytes] = []

    def done(reason: State) -> Capture:
        return Capture(
            pcm=b"".join(blocks),
            reason=reason,
            has_speech=tracker.heard_speech,
            threshold=tracker.threshold,
            peak=tracker.peak,
        )

    while True:
        if cancel is not None and cancel.is_set():
            return done(State.CANCELLED)
        block = read()
        blocks.append(block)
        if tracker.feed(rms(block)) in FINAL:
            return done(tracker.state)


SYSTEM_DEFAULT = "system default"


def order_candidates(
    devices: Sequence[str],
    *,
    default: str | None,
    remembered: str | None,
) -> list[str | None]:
    """Which inputs to try, in order: what already worked, the OS default, then the rest.

    The default comes before any device the user was never asked about, so a
    microphone chosen in Sound settings wins on the first recording of every session.
    The remembered device precedes even that, and only to spare a session that has
    already fallen back from paying the dead default's calibration window on every
    utterance. It leaves the list the moment it goes silent, so the search starts
    from the default again and a microphone fixed mid-session is picked straight up.
    """
    order: list[str | None] = []
    for name in (remembered, default):
        if name is not None and name in devices and name not in order:
            order.append(name)
    order.extend(name for name in devices if name not in order)
    return order or [None]


class MicrophoneRecorder:
    """Records an input device until the tracker finishes, moving off one that is mute.

    The device a session settles on is remembered on the instance and nowhere else.
    `listen` is served by a process that lives as long as the session, so it pays for
    the search once; `/talk` is a fresh process per utterance and searches every time,
    which costs one calibration window per dead device ahead of a live one. Keeping it
    out of the filesystem is deliberate: a remembered device that outlived the session
    would silently override the setting the user can actually see.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        block_ms: float = BLOCK_MS,
        device: str | int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._block_frames = int(sample_rate * block_ms / 1000)
        # An explicitly named device is never fallen back off: the user asked for that
        # microphone, and quietly recording from a different one would be worse than
        # saying the named one is mute.
        self._device = device
        self._remembered: str | None = None

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

        deaf: list[str] = []
        failures: list[RecorderUnavailable] = []
        for candidate in self._candidates(sounddevice):
            try:
                captured = self._capture(sounddevice, candidate, settings, cancel)
            except RecorderUnavailable as error:
                if self._device is not None:
                    raise
                # A remembered device unplugged mid-session lands here; the next
                # candidate is the answer, not an error the user has to act on.
                logger.debug("skipping %s: %s", _label(candidate), error)
                failures.append(error)
                continue
            if captured.reason is not State.DEAF:
                self._remembered = candidate if isinstance(candidate, str) else None
                return Recording(
                    wav=to_wav(captured.pcm, self._sample_rate),
                    reason=captured.reason,
                    has_speech=captured.has_speech,
                    device=candidate,
                    threshold=captured.threshold,
                    peak=captured.peak,
                )
            if self._remembered == candidate:
                self._remembered = None
            deaf.append(_label(candidate))
            logger.info("%s delivered only silence; trying the next input", _label(candidate))

        if deaf:
            raise RecorderUnavailable(_silence_hint(deaf, explicit=self._device is not None))
        if failures:
            raise failures[0]
        raise RecorderUnavailable("no input device was available to record from.")

    def _capture(
        self,
        sounddevice: ModuleType,
        device: str | int | None,
        settings: TrackerSettings,
        cancel: threading.Event,
    ) -> Capture:
        # Constructing the stream already claims the device, and sounddevice streams
        # have no __del__ - so from here on every path out must close it.
        stream = self._open(sounddevice, device)
        with contextlib.closing(stream):
            try:
                stream.start()
            except sounddevice.PortAudioError as error:
                raise RecorderUnavailable(_microphone_hint(error)) from error
            try:
                return drive(lambda: bytes(stream.read(self._block_frames)[0]), settings, cancel)
            except sounddevice.PortAudioError as error:
                raise RecorderUnavailable(
                    f"the microphone failed during recording: {error}. "
                    "Check that the input device is still connected."
                ) from error

    def _candidates(self, sounddevice: ModuleType) -> list[str | int | None]:
        if self._device is not None:
            return [self._device]
        try:
            devices = [
                str(device["name"])
                for device in sounddevice.query_devices()
                if device["max_input_channels"] >= 1
            ]
            default = _default_input(sounddevice, devices)
        except Exception as error:  # a binding too old to enumerate can still record
            logger.debug("could not enumerate input devices: %s", error)
            return [None]
        return list(order_candidates(devices, default=default, remembered=self._remembered))

    def _open(self, sounddevice: ModuleType, device: str | int | None) -> Any:
        try:
            return sounddevice.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=self._block_frames,
                dtype="int16",
                channels=1,
                device=device,
            )
        except Exception as error:  # PortAudioError, or ValueError for a bad device
            raise RecorderUnavailable(_microphone_hint(error)) from error


def _default_input(sounddevice: ModuleType, devices: Sequence[str]) -> str | None:
    """The name of the OS default input, when it is one of the devices we can name."""
    try:
        index = sounddevice.default.device[0]
        name = str(sounddevice.query_devices(index)["name"])
    except Exception as error:  # no default configured, or an index that no longer exists
        logger.debug("no default input device: %s", error)
        return None
    return name if name in devices else None


def describe(recording: Recording) -> str:
    """One line naming the device used and what the gate made of it.

    Stdout carries the transcript, so this goes to the log: it is the difference
    between "heard nothing" as a mystery and as a number the user can act on.
    """
    return (
        f"{_label(recording.device)}: {recording.reason.value} "
        f"(gate {recording.threshold:.4f}, peak {recording.peak:.4f})"
    )


def _label(device: str | int | None) -> str:
    return SYSTEM_DEFAULT if device is None else str(device)


def _silence_hint(tried: Sequence[str], *, explicit: bool) -> str:
    """Say that the microphones are mute, which is not what "heard nothing" means."""
    named = ", ".join(tried)
    if explicit:
        return (
            f"SPEAK_INPUT_DEVICE names {named}, and it delivers only digital silence. "
            "A live microphone always carries a noise floor, so that input is muted or "
            "its volume is at zero. Fix it in System Settings > Sound > Input, or unset "
            "SPEAK_INPUT_DEVICE to search the other inputs."
        )
    return (
        f"no microphone is delivering any audio. Tried: {named}. Every one returned "
        "digital silence, which is not a quiet room - it means the input is muted or its "
        "volume is at zero. Check System Settings > Sound > Input, and that the app running "
        "Claude Code is allowed the microphone under Privacy & Security."
    )


def _microphone_hint(error: BaseException) -> str:
    return (
        f"could not open the microphone: {error}. "
        "On macOS, grant microphone access to the app running Claude Code in "
        "System Settings > Privacy & Security > Microphone."
    )
