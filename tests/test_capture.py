import array
import io
import sys
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest

from speak.capture import (
    FINAL,
    MicrophoneRecorder,
    RecorderUnavailable,
    Recording,
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
        tracker.feed(0.02)
    assert tracker.threshold == 0.06
    assert tracker.state is State.WAITING


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
    pcm, reason = drive(lambda: next(blocks), LIMITS)
    assert reason is State.DONE
    assert len(pcm) == 13 * 960


def test_drive_reports_a_recording_that_was_silence_throughout():
    blocks = iter([block(0)] * 100)
    _pcm, reason = drive(lambda: next(blocks), LIMITS)
    assert reason is State.SILENT


def test_a_silent_recording_has_no_speech():
    assert Recording(wav=b"", reason=State.SILENT).has_speech is False
    assert Recording(wav=b"", reason=State.DONE).has_speech is True
    assert Recording(wav=b"", reason=State.CAPPED).has_speech is True


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
