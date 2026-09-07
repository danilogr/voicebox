"""What the composition root decides: who gets a speech server, and with which models."""

from speak.__main__ import build_ears, build_supervisor
from speak.config import Settings


def test_a_plain_say_session_needs_no_speech_server():
    assert build_supervisor(Settings()) is None


def test_listening_starts_the_speech_server_whatever_the_speech_engine_is():
    assert build_supervisor(Settings(listen=True)) is not None


def test_autostart_off_leaves_the_speech_server_to_the_user():
    assert build_supervisor(Settings(listen=True, nemo_autostart=False)) is None
    assert build_supervisor(Settings(engine="nemo", nemo_autostart=False)) is None


def test_a_session_that_never_listens_is_served_no_asr_model():
    """A missing transcription model must not break a session that only speaks."""
    supervisor = build_supervisor(Settings(engine="nemo"))
    assert supervisor is not None
    assert "--asr-model" not in supervisor._server_args()


def test_a_listening_session_is_served_the_configured_asr_model():
    supervisor = build_supervisor(Settings(listen=True, nemo_asr_model="parakeet-tdt"))
    assert supervisor is not None
    args = supervisor._server_args()
    assert args[args.index("--asr-model") + 1] == "parakeet-tdt"


def test_there_are_no_ears_when_voice_input_is_off():
    assert build_ears(Settings()) is None


def test_the_ears_carry_the_configured_capture_limits():
    ears = build_ears(Settings(listen=True, listen_max_seconds=5.0, listen_timeout=2.0))
    assert ears is not None
    assert ears.limits.max_ms == 5_000
    assert ears.limits.start_timeout_ms == 2_000
