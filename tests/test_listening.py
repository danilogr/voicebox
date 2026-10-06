"""The listening wiring both of this plugin's entry points share."""

from pathlib import Path

from speak_core.engines.asr import DEFAULT_TIMEOUT

from speak.capture import SilenceTracker, State
from speak.cue import END_SOUND, START_SOUND
from speak.listening import build_cue, build_recorder, capture_limits, transcription_timeout
from speak.settings import Settings


def test_capture_limits_turns_the_configured_seconds_into_milliseconds():
    limits = capture_limits(
        Settings(listen_silence_ms=800.0, listen_timeout=5.0, listen_max_seconds=30.0)
    )
    assert limits.silence_ms == 800.0
    assert limits.start_timeout_ms == 5_000.0
    assert limits.max_ms == 30_000.0


def test_the_transcription_timeout_outlasts_the_longest_recording():
    """A fixed 30 s could not transcribe a one-minute answer against a 240 s cap, and
    the failure arrives as "could not transcribe" with the audio already gone."""
    assert transcription_timeout(Settings()) > Settings().listen_max_seconds
    assert transcription_timeout(Settings(listen_max_seconds=600.0)) > 600.0


def test_configured_limits_still_let_a_silent_room_give_up():
    """A gate of zero would latch on the first block: energy is never below it. The
    room is quiet, not muted - exact zeros would be a dead device, not a silent one."""
    tracker = SilenceTracker(capture_limits(Settings()))
    for _ in range(1_000):
        if tracker.feed(0.0005) is State.SILENT:
            break
    assert tracker.state is State.SILENT


def test_the_cue_is_on_by_default_and_uses_the_system_sounds():
    cue = build_cue(Settings())
    assert cue._enabled is True
    assert (cue._start, cue._end) == (START_SOUND, END_SOUND)


def test_configured_cue_sounds_reach_the_cue():
    """The defaults are macOS paths, so the override is the only way off it."""
    cue = build_cue(Settings(listen_cue_start=Path("/sounds/in.wav")))
    assert cue._start == Path("/sounds/in.wav")
    assert cue._end == END_SOUND


def test_a_cue_that_is_turned_off_is_still_built():
    assert build_cue(Settings(listen_cue=False))._enabled is False


def test_the_configured_empty_capture_sound_reaches_the_cue():
    cue = build_cue(Settings(listen_cue_empty=Path("/sounds/nothing.wav")))
    assert cue._empty == Path("/sounds/nothing.wav")


def test_a_named_input_device_reaches_the_recorder():
    assert build_recorder(Settings(input_device="USB Headset"))._device == "USB Headset"


def test_an_unnamed_input_device_leaves_the_recorder_searching():
    assert build_recorder(Settings())._device is None


def test_the_default_timeout_outlasts_the_longest_recording_allowed():
    """Both entry points derive their own, but a caller that does not must not get a
    timeout the configured cap can walk straight through."""
    assert Settings().listen_max_seconds < DEFAULT_TIMEOUT
