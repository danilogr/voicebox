"""What the composition root decides: whether this session may start a speech server."""

from speak_core.config import ServerSettings

from voicebox.__main__ import build_supervisor
from voicebox.config import Settings


def test_a_session_may_start_the_shared_speech_server():
    assert build_supervisor(Settings()) is not None


def test_autostart_off_leaves_the_speech_server_to_the_user():
    assert build_supervisor(Settings(server=ServerSettings(autostart=False))) is None


def test_the_server_is_built_from_the_shared_settings_alone():
    supervisor = build_supervisor(Settings(server=ServerSettings(asr_model="parakeet-tdt")))
    assert supervisor is not None
    args = supervisor._server_args()
    assert args[args.index("--asr-model") + 1] == "parakeet-tdt"
