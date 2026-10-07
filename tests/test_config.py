"""voicebox's settings: a VOICEBOX_ voice, the shared server, and its own state dir."""

from pathlib import Path

from voicebox.config import DEFAULT_ENGINE, Settings


def test_the_voice_is_read_under_voicebox():
    settings = Settings.from_env({"VOICEBOX_VOICE": "Sofia", "SPEAK_VOICE": "Samantha"})
    assert settings.speech.voice == "Sofia"
    assert settings.speech.prefix == "VOICEBOX"


def test_the_voice_defaults_to_the_neural_engine():
    assert Settings.from_env({}).speech.engine == DEFAULT_ENGINE == "nemo"
    assert Settings().speech.engine == "nemo"


def test_an_engine_can_still_be_chosen():
    assert Settings.from_env({"VOICEBOX_ENGINE": "say"}).speech.engine == "say"


def test_the_server_settings_are_the_shared_ones():
    settings = Settings.from_env({"NEMO_HTTP_URL": "http://127.0.0.1:9000"})
    assert settings.server.port == 9000


def test_state_dir_defaults_to_the_mac_cache_directory():
    settings = Settings.from_env({"HOME": "/Users/x"}, platform="darwin")
    assert settings.state_dir == Path("/Users/x/Library/Caches/voicebox")


def test_state_dir_follows_xdg_elsewhere():
    settings = Settings.from_env({"HOME": "/home/x", "XDG_CACHE_HOME": "/c"}, platform="linux")
    assert settings.state_dir == Path("/c/voicebox")


def test_state_dir_can_be_overridden():
    assert Settings.from_env({"VOICEBOX_STATE_DIR": "/tmp/vb"}).state_dir == Path("/tmp/vb")


def test_the_plugin_and_shared_state_dirs_are_different_directories():
    settings = Settings.from_env({"HOME": "/Users/x"}, platform="darwin")
    assert settings.state_dir != settings.server.shared_dir


def test_transcription_settings_are_read_under_voicebox():
    settings = Settings.from_env(
        {"VOICEBOX_TRANSCRIBE_DIR": "/out", "VOICEBOX_TRANSCRIBE_TIMEOUT": "120"}
    )
    assert (settings.transcribe_dir, settings.transcribe_timeout) == (Path("/out"), 120.0)
