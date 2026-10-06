"""This plugin's telemetry session: the install, then its own `sessions` row - what
both `__main__` and `talk` hand their `TelemetrySession` from."""

from speak_core.config import ServerSettings, VoiceSettings
from speak_core.telemetry import store

from speak.settings import Settings
from speak.telemetry import compose


def env(tmp_path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **extra}


def test_build_returns_none_when_telemetry_is_off(tmp_path) -> None:
    assert (
        compose.build(
            env(tmp_path, SPEAK_TELEMETRY="off"),
            settings=Settings(),
            plugin_version="1.1.0",
            host_platform="linux",
        )
        is None
    )


def test_build_writes_a_session_row_naming_the_engines(tmp_path) -> None:
    session = compose.build(
        env(tmp_path, CLAUDE_PLUGIN_ROOT="/plugin"),
        settings=Settings(
            speech=VoiceSettings(prefix="SPEAK", engine="nemo"),
            server=ServerSettings(tts_model="magpie", asr_model="parakeet"),
        ),
        plugin_version="1.2.0",
        host_platform="linux",
    )
    assert session is not None
    rows = store.sessions_by_id(session.conn, [session.session_id])
    assert len(rows) == 1
    assert rows[0]["tts_engine"] == "nemo"
    assert rows[0]["asr_engine"] == "nemo"
    assert bool(rows[0]["listen_enabled"]) is True
    assert rows[0]["harness"] == "claude-code"

    assert session.tts_model == "magpie"
    assert session.asr_model == "parakeet"


def test_each_session_gets_a_fresh_session_id(tmp_path) -> None:
    e = env(tmp_path)
    first = compose.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    second = compose.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    assert first is not None
    assert second is not None
    assert first.session_id != second.session_id


def test_an_overlong_engine_name_is_cut_to_the_contract_length(tmp_path) -> None:
    session = compose.build(
        env(tmp_path),
        settings=Settings(speech=VoiceSettings(prefix="SPEAK", engine="x" * 80)),
        plugin_version="1.1.0",
        host_platform="linux",
    )
    assert session is not None
    (row,) = store.sessions_by_id(session.conn, [session.session_id])
    assert row["tts_engine"] == "x" * compose.TTS_ENGINE_MAX_LENGTH
    assert session.tts_engine == row["tts_engine"]
