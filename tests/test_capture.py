import array
import asyncio
import io
import sys
import threading
import time
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest

from speak.capture import (
    FINAL,
    MicrophoneRecorder,
    RecorderUnavailable,
    SilenceTracker,
    State,
    TrackerSettings,
    drive,
    rms,
    to_wav,
)

LIMITS = TrackerSettings(
    block_ms=100.0,
    calibrate_ms=300.0,
    silence_ms=500.0,
    start_timeout_ms=2000.0,
    max_ms=5000.0,
    noise_multiple=3.0,
    floor=0.01,
)

# Under LIMITS the start timeout always fires before the cap, so the cap is only
# ever reachable from SPEAKING. These limits wait long enough for a speaker that
# the cap arrives first, which is the only way to see it bound a silent phase.
PATIENT = replace(LIMITS, start_timeout_ms=9000.0)

# What `listen(max_seconds=8)` builds against the default 10 s start timeout: the
# cap is the shorter of the two, so an empty room runs into the cap, not the timeout.
CAP_BEFORE_TIMEOUT = replace(LIMITS, start_timeout_ms=10_000.0, max_ms=8000.0)


def run(energies: list[float], limits: TrackerSettings = LIMITS) -> tuple[State, int]:
    """Feed blocks until a final state, reporting it and the block it landed on."""
    tracker = SilenceTracker(limits)
    state = tracker.state
    blocks = 0
    for energy in energies:
        blocks += 1
        state = tracker.feed(energy)
        if state in FINAL:
            break
    return state, blocks


def test_a_quiet_room_calibrates_to_the_floor():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.0)
    assert tracker.threshold == LIMITS.floor
    assert tracker.state is State.WAITING


def test_a_noisy_room_calibrates_above_the_floor():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.01)
    assert tracker.threshold == pytest.approx(0.03)
    assert tracker.state is State.WAITING


def test_talking_through_calibration_does_not_poison_the_gate():
    """Nothing cues the user to wait, so calibrating against their voice is the
    first-run case. An unclamped gate lands near three times speech level and the
    recording ends SILENT - the symptom the docs teach means a denied microphone."""
    state, _blocks = run([0.2] * 10 + [0.0] * 10)
    assert state is State.DONE


def test_the_gate_is_clamped_from_above():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.2)
    assert tracker.threshold == LIMITS.ceiling


def test_silence_throughout_gives_up_once_the_start_timeout_passes():
    assert run([0.0] * 100) == (State.SILENT, 20)


def test_speech_followed_by_silence_finishes():
    assert run([0.0] * 3 + [0.5] * 5 + [0.0] * 5) == (State.DONE, 13)


def test_a_short_pause_does_not_end_the_utterance():
    tracker = SilenceTracker(LIMITS)
    for energy in [0.0] * 3 + [0.5] * 3 + [0.0] * 4 + [0.5] + [0.0] * 4 + [0.5]:
        state = tracker.feed(energy)
    assert state is State.SPEAKING


def test_speech_that_never_stops_hits_the_cap():
    assert run([0.0] * 3 + [0.5] * 100) == (State.CAPPED, 50)


def test_the_cap_also_bounds_a_recording_that_never_starts():
    assert run([0.0] * 100, PATIENT) == (State.CAPPED, 50)


def test_a_final_state_is_sticky():
    tracker = SilenceTracker(LIMITS)
    for _ in range(20):
        tracker.feed(0.0)
    assert tracker.state is State.SILENT
    for _ in range(5):
        assert tracker.feed(0.0) is State.SILENT


def block(amplitude: int, samples: int = 480) -> bytes:
    return array.array("h", [amplitude] * samples).tobytes()


def test_rms_of_silence_is_zero():
    assert rms(block(0)) == 0.0


def test_rms_of_a_full_scale_block_is_about_one():
    assert rms(block(32767)) == pytest.approx(1.0, abs=0.001)


def test_rms_of_an_empty_block_is_zero():
    assert rms(b"") == 0.0


def test_to_wav_produces_a_readable_mono_16khz_file():
    audio = to_wav(block(1000), 16_000)
    with wave.open(io.BytesIO(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 480


def test_drive_returns_the_audio_and_why_it_stopped():
    blocks = iter([block(0)] * 3 + [block(20000)] * 5 + [block(0)] * 5)
    pcm, reason, heard_speech = drive(lambda: next(blocks), LIMITS)
    assert reason is State.DONE
    assert heard_speech is True
    assert len(pcm) == 13 * 960


def test_drive_stops_when_it_is_told_to_and_keeps_what_it_heard():
    """One block of latency: the check is per block, so a blocked read finishes first."""
    cancel = threading.Event()
    blocks = iter([block(0)] * 100)

    def read() -> bytes:
        heard = next(blocks)
        cancel.set()
        return heard

    pcm, reason, _heard_speech = drive(read, LIMITS, cancel)
    assert reason is State.CANCELLED
    assert len(pcm) == 960


def test_drive_that_is_cancelled_before_it_starts_reads_nothing():
    cancel = threading.Event()
    cancel.set()

    def read() -> bytes:
        raise AssertionError("the device must not be read after cancellation")

    assert drive(read, LIMITS, cancel) == (b"", State.CANCELLED, False)


def test_drive_reports_a_recording_that_was_silence_throughout():
    blocks = iter([block(0)] * 100)
    _pcm, reason, heard_speech = drive(lambda: next(blocks), LIMITS)
    assert reason is State.SILENT
    assert heard_speech is False


def test_the_cap_does_not_turn_a_silent_recording_into_speech():
    """The two halves the suite used to pin apart: `feed` checks the cap before the
    start timeout, so an empty room ends CAPPED whenever the cap is the shorter of
    the two - and CAPPED used to mean speech. That combination sends room tone to
    the transcriber and returns its stock phrase as the user's own words."""
    silent_room = iter([block(0)] * 200)
    _pcm, reason, heard_speech = drive(lambda: next(silent_room), CAP_BEFORE_TIMEOUT)
    assert reason is State.CAPPED
    assert heard_speech is False


def test_a_recording_capped_mid_sentence_still_counts_as_speech():
    blocks = iter([block(0)] * 3 + [block(20000)] * 200)
    _pcm, reason, heard_speech = drive(lambda: next(blocks), LIMITS)
    assert reason is State.CAPPED
    assert heard_speech is True


class FakePortAudioError(Exception):
    """Stands in for sounddevice.PortAudioError, which only the real module defines."""


class FakeStream:
    """A RawInputStream that yields silence, without touching audio hardware."""

    def __init__(self, fail_at: str | None = None) -> None:
        self._fail_at = fail_at
        self.started = False
        self.closed = False

    def start(self) -> None:
        if self._fail_at == "start":
            raise FakePortAudioError("device unavailable")
        self.started = True

    def read(self, frames: int) -> tuple[bytes, bool]:
        if self._fail_at == "read":
            raise FakePortAudioError("device disconnected")
        return block(0, frames), False

    def close(self) -> None:
        self.closed = True


def fake_binding(monkeypatch, stream: FakeStream | None = None, opening=None) -> FakeStream:
    """Install a stand-in `sounddevice` so `_record` never opens a real device."""
    stream = stream or FakeStream()
    module = SimpleNamespace(
        PortAudioError=FakePortAudioError,
        RawInputStream=opening or (lambda **_kwargs: stream),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return stream


async def test_a_recorded_stream_is_wrapped_as_a_wav_and_closed(monkeypatch):
    stream = fake_binding(monkeypatch)
    recording = await MicrophoneRecorder().record(LIMITS)
    assert recording.reason is State.SILENT
    assert stream.started is True
    assert stream.closed is True
    with wave.open(io.BytesIO(recording.wav), "rb") as handle:
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 20 * 480


async def test_a_capped_recording_of_an_empty_room_reports_no_speech(monkeypatch):
    """The join: a silent recording must never reach the transcriber, whichever
    limit ended it. `listen(max_seconds=8)` with the default 10 s start timeout is
    the ordinary way to land here."""
    fake_binding(monkeypatch)
    recording = await MicrophoneRecorder().record(CAP_BEFORE_TIMEOUT)
    assert recording.reason is State.CAPPED
    assert recording.has_speech is False


async def test_a_stream_that_cannot_be_opened_is_reported(monkeypatch):
    def explode(**_kwargs: object) -> FakeStream:
        raise FakePortAudioError("Error querying device 99")

    fake_binding(monkeypatch, opening=explode)
    with pytest.raises(RecorderUnavailable, match="could not open the microphone"):
        await MicrophoneRecorder().record(LIMITS)


async def test_a_stream_that_cannot_start_is_reported_and_still_closed(monkeypatch):
    stream = fake_binding(monkeypatch, FakeStream(fail_at="start"))
    with pytest.raises(RecorderUnavailable, match="could not open the microphone"):
        await MicrophoneRecorder().record(LIMITS)
    assert stream.closed is True


async def test_a_device_lost_mid_recording_is_reported_and_still_closed(monkeypatch):
    stream = fake_binding(monkeypatch, FakeStream(fail_at="read"))
    with pytest.raises(RecorderUnavailable, match="failed during recording"):
        await MicrophoneRecorder().record(LIMITS)
    assert stream.closed is True


async def test_a_missing_binding_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(RecorderUnavailable, match="no microphone binding"):
        await MicrophoneRecorder().record(LIMITS)


class SlowStream(FakeStream):
    """Delivers its blocks in real time, the way a device paces a recording."""

    def __init__(self, block_seconds: float = 0.02) -> None:
        super().__init__()
        self._block_seconds = block_seconds
        self.reading = threading.Event()

    def read(self, frames: int) -> tuple[bytes, bool]:
        self.reading.set()
        time.sleep(self._block_seconds)
        return block(0, frames), False


async def closes_within(stream: FakeStream, timeout: float) -> bool:
    """True once the recording thread has let the device go."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if stream.closed:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_a_cancelled_recording_lets_the_microphone_go(monkeypatch):
    """Cancelling the await returns at once; the thread has to be told as well.

    Otherwise it records the whole cap out with the device open: the next `listen`
    opens a second stream against it, and interpreter shutdown joins the thread.
    """
    stream = fake_binding(monkeypatch, SlowStream())
    # Limits that only a cancellation can end early: 600 blocks at 20 ms apiece.
    patient = replace(LIMITS, start_timeout_ms=60_000.0, max_ms=60_000.0)
    recording = asyncio.create_task(MicrophoneRecorder().record(patient))
    assert await asyncio.to_thread(stream.reading.wait, 5.0) is True

    recording.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recording

    assert await closes_within(stream, 5.0) is True
