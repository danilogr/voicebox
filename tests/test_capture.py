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
    Recording,
    SilenceTracker,
    State,
    TrackerSettings,
    describe,
    drive,
    order_candidates,
    rms,
    to_wav,
)

# A real microphone in a silent room still carries a noise floor; only a muted one
# reads as exact zero. The suite used 0.0 for both, which is the confusion that let a
# muted default device spend the whole start timeout and then report "heard nothing".
QUIET = 0.0005

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
        tracker.feed(QUIET)
    assert tracker.threshold == LIMITS.floor
    assert tracker.state is State.WAITING


def test_a_noisy_room_calibrates_above_the_floor():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.01)
    assert tracker.threshold == pytest.approx(0.03)
    assert tracker.state is State.WAITING


def test_talking_through_calibration_does_not_poison_the_gate():
    """Nothing cues the user to wait, so calibrating against their own voice is the
    ordinary first run. An unclamped gate lands near three times speech level, which no
    microphone reaches, and the utterance is lost with nothing to show for it."""
    state, _blocks = run([0.2] * 10 + [0.0] * 10)
    assert state is State.DONE


def test_the_gate_is_clamped_from_above():
    """A noisy room, not a voice: every block stays under the ceiling while the computed
    gate would exceed it. A block as loud as a voice starts the utterance instead."""
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.02)
    assert tracker.state is State.WAITING
    assert tracker.threshold == LIMITS.ceiling


def test_silence_throughout_gives_up_once_the_start_timeout_passes():
    assert run([QUIET] * 100) == (State.SILENT, 20)


def test_speech_followed_by_silence_finishes():
    assert run([QUIET] * 3 + [0.5] * 5 + [0.0] * 5) == (State.DONE, 13)


def test_a_short_pause_does_not_end_the_utterance():
    tracker = SilenceTracker(LIMITS)
    for energy in [QUIET] * 3 + [0.5] * 3 + [0.0] * 4 + [0.5] + [0.0] * 4 + [0.5]:
        state = tracker.feed(energy)
    assert state is State.SPEAKING


def test_speech_that_never_stops_hits_the_cap():
    assert run([QUIET] * 3 + [0.5] * 100) == (State.CAPPED, 50)


def test_the_cap_also_bounds_a_recording_that_never_starts():
    assert run([QUIET] * 100, PATIENT) == (State.CAPPED, 50)


def test_a_final_state_is_sticky():
    tracker = SilenceTracker(LIMITS)
    for _ in range(20):
        tracker.feed(QUIET)
    assert tracker.state is State.SILENT
    for _ in range(5):
        assert tracker.feed(QUIET) is State.SILENT


def test_a_muted_device_is_deaf_rather_than_silent():
    """Exact zeros are not a quiet room. Calling them one costs the whole start
    timeout and then blames the user for not speaking, when the input is muted."""
    tracker = SilenceTracker(LIMITS)
    for _ in range(int(LIMITS.deaf_ms / LIMITS.block_ms)):
        tracker.feed(0.0)
    assert tracker.state is State.DEAF


def test_deafness_is_decided_before_the_start_timeout_runs():
    assert run([0.0] * 100) == (State.DEAF, 15)


def test_a_device_that_wakes_late_is_not_called_mute():
    """CoreAudio hands over exact zeros while an input primes, and a Bluetooth
    microphone can take most of a second. Judging deafness inside the 300 ms
    calibration window rejects a working microphone that is merely slow to start."""
    state, _blocks = run([0.0] * 6 + [QUIET] * 3 + [0.5] * 3 + [0.0] * 6)
    assert state is State.DONE


def test_a_device_still_silent_after_the_warmup_window_is_mute():
    """The window has to end somewhere, or a dead default holds up every recording."""
    tracker = SilenceTracker(LIMITS)
    for _ in range(14):
        assert tracker.feed(0.0) is State.WARMING
    assert tracker.feed(0.0) is State.DEAF


def test_priming_silence_stays_out_of_the_gate():
    """Calibration runs from the first real sample, not from the stream opening. An
    ambient average diluted by priming zeros lands the gate at the floor, and in a
    noisy room a floor-level gate opens on the room and never closes again."""
    tracker = SilenceTracker(LIMITS)
    for _ in range(6):
        tracker.feed(0.0)
    for _ in range(3):
        tracker.feed(0.01)
    assert tracker.state is State.WAITING
    assert tracker.threshold == pytest.approx(0.03)


def test_a_word_spoken_before_the_gate_arms_starts_the_utterance():
    """Calibration runs 300 ms from the first real sample, and a one-word answer can be
    over before it ends. Averaging that voice into the ambient raises the gate above it,
    and the blocks are already buffered, so the utterance begins there."""
    tracker = SilenceTracker(LIMITS)
    tracker.feed(QUIET)
    assert tracker.feed(0.3) is State.SPEAKING
    assert tracker.heard_speech is True
    assert tracker.threshold == LIMITS.floor


def test_an_early_word_still_ends_the_recording_on_silence():
    assert run([QUIET] + [0.3] * 2 + [0.0] * 5) == (State.DONE, 8)


def test_the_audio_of_an_early_word_is_kept_for_the_transcriber():
    """Two loud blocks, so the word is over before calibration would have ended. With a
    third, the clamped gate arms in time to catch it and the test passes either way."""
    blocks = iter([block(1)] + [block(20000)] * 2 + [block(0)] * 25)
    captured = drive(lambda: next(blocks), LIMITS)
    assert captured.reason is State.DONE
    assert captured.has_speech is True
    assert len(captured.pcm) == 8 * 960


def test_the_peak_is_carried_so_a_failure_can_say_how_close_it_came():
    tracker = SilenceTracker(LIMITS)
    for energy in (QUIET, 0.008, QUIET):
        tracker.feed(energy)
    assert tracker.peak == pytest.approx(0.008)


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
    blocks = iter([block(1)] * 3 + [block(20000)] * 5 + [block(0)] * 5)
    captured = drive(lambda: next(blocks), LIMITS)
    assert captured.reason is State.DONE
    assert captured.has_speech is True
    assert len(captured.pcm) == 13 * 960


def test_drive_stops_when_it_is_told_to_and_keeps_what_it_heard():
    """One block of latency: the check is per block, so a blocked read finishes first."""
    cancel = threading.Event()
    blocks = iter([block(0)] * 100)

    def read() -> bytes:
        heard = next(blocks)
        cancel.set()
        return heard

    captured = drive(read, LIMITS, cancel)
    assert captured.reason is State.CANCELLED
    assert len(captured.pcm) == 960


def test_drive_that_is_cancelled_before_it_starts_reads_nothing():
    cancel = threading.Event()
    cancel.set()

    def read() -> bytes:
        raise AssertionError("the device must not be read after cancellation")

    captured = drive(read, LIMITS, cancel)
    assert (captured.pcm, captured.reason, captured.has_speech) == (b"", State.CANCELLED, False)


def test_drive_reports_a_recording_that_was_silence_throughout():
    blocks = iter([block(1)] * 100)
    captured = drive(lambda: next(blocks), LIMITS)
    assert captured.reason is State.SILENT
    assert captured.has_speech is False


def test_drive_reports_a_muted_device_apart_from_a_silent_room():
    blocks = iter([block(0)] * 100)
    captured = drive(lambda: next(blocks), LIMITS)
    assert captured.reason is State.DEAF
    assert captured.has_speech is False


def test_the_cap_does_not_turn_a_silent_recording_into_speech():
    """`feed` checks the cap before the start timeout, so an empty room ends CAPPED
    whenever the cap is the shorter of the two. CAPPED must therefore not imply speech:
    room tone reaching the transcriber comes back as its stock phrase, which the caller
    then treats as the user's own words."""
    silent_room = iter([block(1)] * 200)
    captured = drive(lambda: next(silent_room), CAP_BEFORE_TIMEOUT)
    assert captured.reason is State.CAPPED
    assert captured.has_speech is False


def test_a_recording_capped_mid_sentence_still_counts_as_speech():
    blocks = iter([block(1)] * 3 + [block(20000)] * 200)
    captured = drive(lambda: next(blocks), LIMITS)
    assert captured.reason is State.CAPPED
    assert captured.has_speech is True


class FakePortAudioError(Exception):
    """Stands in for sounddevice.PortAudioError, which only the real module defines."""


class FakeStream:
    """A RawInputStream that yields silence, without touching audio hardware."""

    def __init__(self, fail_at: str | None = None, amplitude: int = 1) -> None:
        self._fail_at = fail_at
        # Non-zero by default: a live microphone always has a noise floor, and a
        # stream of exact zeros now means a muted device rather than a quiet room.
        self._amplitude = amplitude
        self.started = False
        self.closed = False

    def start(self) -> None:
        if self._fail_at == "start":
            raise FakePortAudioError("device unavailable")
        self.started = True

    def read(self, frames: int) -> tuple[bytes, bool]:
        if self._fail_at == "read":
            raise FakePortAudioError("device disconnected")
        return block(self._amplitude, frames), False

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
        return block(self._amplitude, frames), False


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


# --- choosing which input to record from ---

DEVICES = ["Built-in Mic", "USB Headset", "Webcam"]


def test_the_os_default_leads_the_search():
    """The setting the user can see wins on the first recording of every session."""
    assert order_candidates(DEVICES, default="USB Headset", remembered=None) == [
        "USB Headset",
        "Built-in Mic",
        "Webcam",
    ]


def test_a_remembered_device_is_tried_before_the_default():
    """Only so a session that already fell back does not pay the dead default's
    calibration window on every utterance."""
    assert order_candidates(DEVICES, default="Built-in Mic", remembered="Webcam") == [
        "Webcam",
        "Built-in Mic",
        "USB Headset",
    ]


def test_a_remembered_device_that_has_been_unplugged_is_dropped():
    assert order_candidates(DEVICES, default="Built-in Mic", remembered="Desk Mic") == [
        "Built-in Mic",
        "USB Headset",
        "Webcam",
    ]


def test_a_default_that_is_also_the_remembered_one_is_tried_once():
    assert order_candidates(DEVICES, default="Webcam", remembered="Webcam") == [
        "Webcam",
        "Built-in Mic",
        "USB Headset",
    ]


def test_with_nothing_to_enumerate_the_system_default_is_still_offered():
    assert order_candidates([], default=None, remembered=None) == [None]


def fake_inputs(monkeypatch, levels: dict[str, int], default: str) -> list[object]:
    """A sounddevice whose inputs yield the given amplitudes; 0 stands for muted.

    `levels` is read when a stream is opened, so a test can mute or restore a device
    between recordings the way unplugging one does.
    """
    names = list(levels)
    opened: list[object] = []

    def query_devices(index: int | None = None) -> object:
        if index is None:
            return [{"name": name, "max_input_channels": 1} for name in names]
        return {"name": names[index], "max_input_channels": 1}

    def open_stream(**kwargs: object) -> FakeStream:
        device = kwargs["device"]
        opened.append(device)
        return FakeStream(amplitude=levels[str(device)])

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            PortAudioError=FakePortAudioError,
            RawInputStream=open_stream,
            query_devices=query_devices,
            default=SimpleNamespace(device=(names.index(default), 1)),
        ),
    )
    return opened


async def test_a_muted_default_falls_through_to_a_live_input(monkeypatch):
    opened = fake_inputs(monkeypatch, {"Built-in Mic": 0, "USB Headset": 1}, default="Built-in Mic")
    recording = await MicrophoneRecorder().record(LIMITS)
    assert opened == ["Built-in Mic", "USB Headset"]
    assert recording.device == "USB Headset"
    assert recording.reason is State.SILENT


async def test_the_input_that_worked_is_reused_without_searching_again(monkeypatch):
    opened = fake_inputs(monkeypatch, {"Built-in Mic": 0, "USB Headset": 1}, default="Built-in Mic")
    recorder = MicrophoneRecorder()
    await recorder.record(LIMITS)
    opened.clear()
    await recorder.record(LIMITS)
    assert opened == ["USB Headset"]


async def test_a_remembered_input_that_goes_mute_restarts_the_search(monkeypatch):
    """And restarts it from the default, so a microphone fixed mid-session is used."""
    levels = {"Built-in Mic": 0, "USB Headset": 1}
    opened = fake_inputs(monkeypatch, levels, default="Built-in Mic")
    recorder = MicrophoneRecorder()
    await recorder.record(LIMITS)
    levels["USB Headset"] = 0
    levels["Built-in Mic"] = 1
    opened.clear()
    recording = await recorder.record(LIMITS)
    assert opened == ["USB Headset", "Built-in Mic"]
    assert recording.device == "Built-in Mic"


async def test_every_input_muted_says_so_rather_than_reporting_silence(monkeypatch):
    fake_inputs(monkeypatch, {"Built-in Mic": 0, "USB Headset": 0}, default="Built-in Mic")
    with pytest.raises(RecorderUnavailable, match="no microphone is delivering any audio"):
        await MicrophoneRecorder().record(LIMITS)


async def test_a_named_input_is_never_quietly_swapped_for_another(monkeypatch):
    """Recording from a device the user did not ask for is worse than saying theirs
    is mute, so an explicit SPEAK_INPUT_DEVICE ends the search rather than starting it."""
    opened = fake_inputs(monkeypatch, {"Built-in Mic": 0, "USB Headset": 1}, default="Built-in Mic")
    with pytest.raises(RecorderUnavailable, match="SPEAK_INPUT_DEVICE"):
        await MicrophoneRecorder(device="Built-in Mic").record(LIMITS)
    assert opened == ["Built-in Mic"]


async def test_a_binding_that_cannot_enumerate_still_records(monkeypatch):
    fake_binding(monkeypatch)
    recording = await MicrophoneRecorder().record(LIMITS)
    assert recording.device is None


def test_describe_names_the_input_and_what_the_gate_made_of_it():
    recording = Recording(
        wav=b"",
        reason=State.SILENT,
        has_speech=False,
        device="USB Headset",
        threshold=0.0108,
        peak=0.0041,
    )
    assert describe(recording) == "USB Headset: silent (gate 0.0108, peak 0.0041)"
