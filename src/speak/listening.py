"""What both of this plugin's entry points build the same way for listening: the
recording limits, the microphone, the cue, and the transcription timeout."""

from speak.capture import MicrophoneRecorder, TrackerSettings
from speak.cue import Cue
from speak.settings import Settings

# Enough for the request itself on top of the audio. Transcription runs well above
# realtime (see harness/performance.md), so a second of allowance per second of
# recording is generous.
TRANSCRIBE_MARGIN = 30.0


def transcription_timeout(settings: Settings) -> float:
    """How long one transcription may take, derived from the longest recording allowed.

    A fixed timeout cannot serve a configurable cap: at the default 240 seconds the
    old 30-second one could not have transcribed a one-minute answer, and a
    transcription that times out has already discarded the audio and blames the
    server rather than reporting that it gave up.
    """
    return settings.listen_max_seconds + TRANSCRIBE_MARGIN


def capture_limits(settings: Settings) -> TrackerSettings:
    """Turn the configured seconds and milliseconds into one recording's limits."""
    return TrackerSettings(
        silence_ms=settings.listen_silence_ms,
        start_timeout_ms=settings.listen_timeout * 1000,
        max_ms=settings.listen_max_seconds * 1000,
    )


def build_cue(settings: Settings) -> Cue:
    """The chimes that bracket one recording, built the same way by both entry points."""
    return Cue(
        enabled=settings.listen_cue,
        start=settings.listen_cue_start,
        end=settings.listen_cue_end,
        empty=settings.listen_cue_empty,
    )


def build_recorder(settings: Settings) -> MicrophoneRecorder:
    """The microphone, built the same way by both entry points."""
    return MicrophoneRecorder(device=settings.input_device)
