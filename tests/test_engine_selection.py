import pytest

from speak.config import ServerSettings, VoiceSettings
from speak.engines import build_engine
from speak.engines.command import CommandEngine
from speak.engines.nemo import NemoEngine
from speak.engines.say import SayEngine


def engine_for(env, prefix="SPEAK"):
    return build_engine(
        VoiceSettings.from_env(env, prefix=prefix),
        ServerSettings.from_env(env, platform="darwin"),
        on_unavailable=None,
        platform="darwin",
    )


def test_say_is_the_default():
    assert isinstance(engine_for({}), SayEngine)


def test_selects_the_command_engine():
    engine = engine_for({"SPEAK_ENGINE": "command", "SPEAK_COMMAND": "piper"})
    assert isinstance(engine, CommandEngine)


def test_the_command_engine_needs_a_command():
    with pytest.raises(ValueError, match="SPEAK_COMMAND"):
        engine_for({"SPEAK_ENGINE": "command"})


def test_selects_the_nemo_engine():
    assert isinstance(engine_for({"SPEAK_ENGINE": "nemo"}), NemoEngine)


def test_the_nemo_engine_defaults_to_john():
    assert engine_for({"SPEAK_ENGINE": "nemo"}).default_voice == "John"


def test_the_nemo_engine_honours_a_configured_voice():
    engine = engine_for({"SPEAK_ENGINE": "nemo", "SPEAK_VOICE": "Sofia"})
    assert engine.default_voice == "Sofia"


def test_an_unknown_engine_falls_back_to_say(caplog):
    engine = engine_for({"SPEAK_ENGINE": "kazoo"})
    assert isinstance(engine, SayEngine)
    assert "kazoo" in caplog.text


def test_the_say_engine_carries_the_configured_voice():
    assert engine_for({"SPEAK_VOICE": "Samantha"}).default_voice == "Samantha"


def test_the_command_engine_names_the_plugins_own_setting():
    with pytest.raises(ValueError, match="WALKIE_COMMAND"):
        engine_for({"WALKIE_ENGINE": "command"}, prefix="WALKIE")


def test_the_nemo_engine_talks_to_the_shared_server():
    engine = engine_for({"SPEAK_ENGINE": "nemo", "NEMO_HTTP_URL": "http://127.0.0.1:9100"})
    assert engine._base_url == "http://127.0.0.1:9100"
