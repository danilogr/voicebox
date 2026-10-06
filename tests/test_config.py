"""Settings every plugin reads the same way: the shared speech server's, and a voice
read under the plugin's own prefix."""

from pathlib import Path

from speak.config import (
    DEFAULT_NEMO_URL,
    ServerSettings,
    VoiceSettings,
    plugin_state_dir,
)
from speak.text import DEFAULT_MAX_CHARS


def server(env: dict[str, str], platform: str = "darwin") -> ServerSettings:
    return ServerSettings.from_env(env, platform=platform)


def voice(env: dict[str, str], prefix: str = "WALKIE") -> VoiceSettings:
    return VoiceSettings.from_env(env, prefix=prefix)


def test_server_defaults_when_nothing_is_set():
    settings = server({"HOME": "/Users/x"})
    assert settings.url == DEFAULT_NEMO_URL
    assert settings.api_key is None
    assert settings.tts_model == "magpie"
    assert settings.asr_model == "nemotron-3.5"
    assert settings.binary == "nemo-speech"
    assert settings.autostart is True
    assert settings.seed is None
    assert settings.port == 8082


def test_server_settings_come_from_unprefixed_nemo_variables():
    settings = server(
        {
            "NEMO_HTTP_URL": "http://example:9000",
            "NEMO_API_KEY": "secret",
            "NEMO_TTS_MODEL": "other",
            "NEMO_ASR_MODEL": "parakeet-tdt",
            "NEMO_BIN": "/opt/nemo",
            "NEMO_AUTOSTART": "0",
            "NEMO_TTS_SEED": "7",
        }
    )
    assert settings.url == "http://example:9000"
    assert settings.api_key == "secret"
    assert settings.tts_model == "other"
    assert settings.asr_model == "parakeet-tdt"
    assert settings.binary == "/opt/nemo"
    assert settings.autostart is False
    assert settings.seed == 7
    assert settings.port == 9000


def test_a_plugin_prefixed_server_setting_is_not_read():
    """Whichever plugin starts the server decides its arguments for every plugin, so
    no server setting may live under one plugin's prefix."""
    settings = server(
        {"SPEAK_NEMO_BIN": "/opt/nemo", "SPEAK_NEMO_AUTOSTART": "0", "SPEAK_SEED": "7"}
    )
    assert settings.binary == "nemo-speech"
    assert settings.autostart is True
    assert settings.seed is None


def test_autostart_is_disabled_only_by_a_false_value():
    assert server({"NEMO_AUTOSTART": "0"}).autostart is False
    assert server({"NEMO_AUTOSTART": "1"}).autostart is True
    assert server({"NEMO_AUTOSTART": "maybe"}).autostart is True


def test_a_non_finite_seed_is_ignored():
    assert server({"NEMO_TTS_SEED": "inf"}).seed is None


def test_the_port_comes_from_the_url():
    assert server({"NEMO_HTTP_URL": "http://127.0.0.1:9100"}).port == 9100
    assert server({"NEMO_HTTP_URL": "http://127.0.0.1"}).port == 8082


def test_the_shared_state_dir_is_the_mac_cache_directory():
    assert server({"HOME": "/Users/x"}).shared_dir == Path("/Users/x/Library/Caches/speak-core")


def test_the_shared_state_dir_follows_xdg_elsewhere():
    shared = server({"XDG_CACHE_HOME": "/cache"}, platform="linux").shared_dir
    assert shared == Path("/cache/speak-core")


def test_the_shared_state_dir_can_be_overridden():
    assert server({"SPEAK_CORE_STATE_DIR": "/tmp/shared"}).shared_dir == Path("/tmp/shared")


def test_a_plugin_state_dir_override_does_not_move_the_shared_one():
    shared = server({"HOME": "/Users/x", "SPEAK_STATE_DIR": "/tmp/state"}).shared_dir
    assert shared == Path("/Users/x/Library/Caches/speak-core")


def test_a_plugin_state_dir_is_named_for_the_plugin():
    env = {"HOME": "/Users/x"}
    assert plugin_state_dir(env, name="walkie", override="WALKIE_STATE_DIR", platform="darwin") == (
        Path("/Users/x/Library/Caches/walkie")
    )
    assert plugin_state_dir(
        {"XDG_CACHE_HOME": "/cache"}, name="walkie", override="WALKIE_STATE_DIR", platform="linux"
    ) == Path("/cache/walkie")
    assert plugin_state_dir(
        {"WALKIE_STATE_DIR": "/tmp/w"},
        name="walkie",
        override="WALKIE_STATE_DIR",
        platform="darwin",
    ) == Path("/tmp/w")


def test_voice_defaults_when_nothing_is_set():
    settings = voice({})
    assert settings.prefix == "WALKIE"
    assert settings.engine == "say"
    assert settings.voice is None
    assert settings.randomize_voice is False
    assert settings.voice_pool == ()
    assert settings.rate is None
    assert settings.max_chars == DEFAULT_MAX_CHARS
    assert settings.command is None


def test_voice_settings_are_read_under_the_plugins_prefix():
    settings = voice(
        {
            "WALKIE_ENGINE": "Command",
            "WALKIE_VOICE": "Sofia",
            "WALKIE_RATE": "190",
            "WALKIE_MAX_CHARS": "120",
            "WALKIE_COMMAND": "piper --model x",
        }
    )
    assert settings.engine == "command"
    assert settings.voice == "Sofia"
    assert settings.rate == 190
    assert settings.max_chars == 120
    assert settings.command == "piper --model x"


def test_another_plugins_voice_settings_are_ignored():
    settings = voice({"VOICEBOX_ENGINE": "nemo", "SPEAK_VOICE": "Sofia"})
    assert settings.engine == "say"
    assert settings.voice is None


def test_key_names_a_setting_under_the_prefix():
    assert voice({}).key("COMMAND") == "WALKIE_COMMAND"


def test_unparsable_numbers_fall_back_to_the_default():
    settings = voice({"WALKIE_MAX_CHARS": "lots", "WALKIE_RATE": "fast"})
    assert settings.max_chars == DEFAULT_MAX_CHARS
    assert settings.rate is None


def test_a_zero_or_negative_cap_falls_back_to_the_default():
    assert voice({"WALKIE_MAX_CHARS": "0"}).max_chars == DEFAULT_MAX_CHARS
    assert voice({"WALKIE_MAX_CHARS": "-5"}).max_chars == DEFAULT_MAX_CHARS


def test_a_non_finite_number_never_reaches_the_settings():
    assert voice({"WALKIE_MAX_CHARS": "nan"}).max_chars == DEFAULT_MAX_CHARS
    assert voice({"WALKIE_RATE": "nan"}).rate is None


def test_empty_strings_are_treated_as_unset():
    settings = voice({"WALKIE_VOICE": "", "WALKIE_ENGINE": ""})
    assert settings.voice is None
    assert settings.engine == "say"


def test_reads_a_comma_separated_voice_pool():
    assert voice({"WALKIE_VOICE_POOL": "Samantha,Karen,Moira"}).voice_pool == (
        "Samantha",
        "Karen",
        "Moira",
    )


def test_voice_pool_tolerates_spacing_and_empty_entries():
    assert voice({"WALKIE_VOICE_POOL": " Samantha , , Karen "}).voice_pool == ("Samantha", "Karen")


def test_randomize_is_off_unless_the_voice_is_random():
    assert voice({"WALKIE_VOICE": "Samantha"}).randomize_voice is False


def test_random_is_recognised_case_insensitively_and_never_kept_as_a_name():
    settings = voice({"WALKIE_VOICE": "Random"})
    assert settings.randomize_voice is True
    assert settings.voice is None
