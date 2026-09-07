from pathlib import Path

from speak.config import Settings


def test_defaults_when_nothing_is_set():
    settings = Settings.from_env({}, platform="darwin")
    assert settings.engine == "say"
    assert settings.voice is None
    assert settings.rate is None
    assert settings.max_chars == 400
    assert settings.nemo_url == "http://127.0.0.1:8082"
    assert settings.nemo_model == "magpie"
    assert settings.nemo_binary == "nemo-speech"
    assert settings.nemo_autostart is True
    assert settings.seed is None
    assert settings.command is None


def test_engine_name_is_case_insensitive():
    assert Settings.from_env({"SPEAK_ENGINE": "NeMo"}).engine == "nemo"


def test_reads_every_variable():
    settings = Settings.from_env(
        {
            "SPEAK_ENGINE": "command",
            "SPEAK_VOICE": "Sofia",
            "SPEAK_RATE": "190",
            "SPEAK_MAX_CHARS": "120",
            "NEMO_HTTP_URL": "http://example:9000",
            "NEMO_API_KEY": "secret",
            "NEMO_TTS_MODEL": "other",
            "SPEAK_COMMAND": "piper --model x",
            "SPEAK_NEMO_BIN": "/opt/nemo",
            "SPEAK_SEED": "7",
        }
    )
    assert settings.engine == "command"
    assert settings.voice == "Sofia"
    assert settings.rate == 190
    assert settings.max_chars == 120
    assert settings.nemo_url == "http://example:9000"
    assert settings.nemo_api_key == "secret"
    assert settings.nemo_model == "other"
    assert settings.command == "piper --model x"
    assert settings.nemo_binary == "/opt/nemo"
    assert settings.seed == 7


def test_autostart_is_disabled_only_by_zero():
    assert Settings.from_env({"SPEAK_NEMO_AUTOSTART": "0"}).nemo_autostart is False
    assert Settings.from_env({"SPEAK_NEMO_AUTOSTART": "1"}).nemo_autostart is True


def test_unparsable_numbers_fall_back_to_the_default():
    settings = Settings.from_env({"SPEAK_MAX_CHARS": "lots", "SPEAK_RATE": "fast"})
    assert settings.max_chars == 400
    assert settings.rate is None


def test_a_zero_or_negative_cap_falls_back_to_the_default():
    assert Settings.from_env({"SPEAK_MAX_CHARS": "0"}).max_chars == 400
    assert Settings.from_env({"SPEAK_MAX_CHARS": "-5"}).max_chars == 400


def test_empty_strings_are_treated_as_unset():
    settings = Settings.from_env({"SPEAK_VOICE": "", "SPEAK_ENGINE": ""})
    assert settings.voice is None
    assert settings.engine == "say"


def test_state_dir_defaults_to_the_mac_cache_directory():
    settings = Settings.from_env({"HOME": "/Users/x"}, platform="darwin")
    assert settings.state_dir == Path("/Users/x/Library/Caches/speak")


def test_state_dir_follows_xdg_elsewhere():
    settings = Settings.from_env({"XDG_CACHE_HOME": "/cache"}, platform="linux")
    assert settings.state_dir == Path("/cache/speak")


def test_state_dir_can_be_overridden():
    settings = Settings.from_env({"SPEAK_STATE_DIR": "/tmp/state"}, platform="darwin")
    assert settings.state_dir == Path("/tmp/state")


def test_nemo_port_comes_from_the_url():
    assert Settings.from_env({"NEMO_HTTP_URL": "http://127.0.0.1:9100"}).nemo_port == 9100
    assert Settings.from_env({"NEMO_HTTP_URL": "http://127.0.0.1"}).nemo_port == 8082


def test_no_voice_pool_by_default():
    assert Settings.from_env({}).voice_pool == ()


def test_reads_a_comma_separated_voice_pool():
    settings = Settings.from_env({"SPEAK_VOICE_POOL": "Samantha,Karen,Moira"})
    assert settings.voice_pool == ("Samantha", "Karen", "Moira")


def test_voice_pool_tolerates_spacing_and_empty_entries():
    settings = Settings.from_env({"SPEAK_VOICE_POOL": " Samantha , , Karen "})
    assert settings.voice_pool == ("Samantha", "Karen")


def test_randomize_is_off_unless_the_voice_is_random():
    assert Settings.from_env({}).randomize_voice is False
    assert Settings.from_env({"SPEAK_VOICE": "Samantha"}).randomize_voice is False


def test_random_is_recognised_case_insensitively():
    assert Settings.from_env({"SPEAK_VOICE": "Random"}).randomize_voice is True


def test_random_is_never_treated_as_a_literal_voice_name():
    assert Settings.from_env({"SPEAK_VOICE": "random"}).voice is None


def test_listen_is_off_unless_asked_for():
    assert Settings.from_env({}).listen is False


def test_listen_accepts_the_usual_truthy_spellings():
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert Settings.from_env({"SPEAK_LISTEN": raw}).listen is True, raw


def test_listen_treats_anything_else_as_off():
    for raw in ("0", "false", "no", "off", ""):
        assert Settings.from_env({"SPEAK_LISTEN": raw}).listen is False, raw


def test_asr_model_defaults_and_overrides():
    assert Settings.from_env({}).nemo_asr_model == "nemotron-3.5"
    assert Settings.from_env({"NEMO_ASR_MODEL": "parakeet-tdt"}).nemo_asr_model == "parakeet-tdt"


def test_listen_numbers_have_defaults():
    settings = Settings.from_env({})
    assert settings.listen_silence_ms == 1500
    assert settings.listen_max_seconds == 60
    assert settings.listen_timeout == 10


def test_unparsable_listen_numbers_fall_back_to_the_default():
    settings = Settings.from_env({"SPEAK_LISTEN_SILENCE_MS": "soon"})
    assert settings.listen_silence_ms == 1500


def test_non_positive_listen_numbers_fall_back_to_the_default():
    settings = Settings.from_env({"SPEAK_LISTEN_MAX_SECONDS": "-5"})
    assert settings.listen_max_seconds == 60


def test_listen_language_is_optional():
    assert Settings.from_env({}).listen_language is None
    assert Settings.from_env({"SPEAK_LISTEN_LANGUAGE": "en"}).listen_language == "en"
