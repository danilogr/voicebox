"""The wiring both entry points share."""

from speak.capture import SilenceTracker, State
from speak.config import Settings
from speak.runtime import capture_limits, transcription_timeout


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
    """A gate of zero would latch on the first block: energy is never below it."""
    tracker = SilenceTracker(capture_limits(Settings()))
    for _ in range(1_000):
        if tracker.feed(0.0) is State.SILENT:
            break
    assert tracker.state is State.SILENT
