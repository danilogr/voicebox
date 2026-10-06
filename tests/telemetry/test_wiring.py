"""Composition: consent, the machine probe, the local store, and the session row
built from them - what both `__main__` and `talk` hand their `TelemetrySession` from.
"""

import pytest

from speak.config import ServerSettings, VoiceSettings
from speak.settings import Settings
from speak.telemetry import consent, store, wiring
from speak.telemetry.contract import SCHEMA


def env(tmp_path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **extra}


def test_detect_harness_reads_claude_plugin_root() -> None:
    assert wiring.detect_harness({"CLAUDE_PLUGIN_ROOT": "/some/path"}) == ("claude-code", None)


def test_detect_harness_is_honestly_unknown_without_a_signal() -> None:
    """spec 2: "unknown" is a real value that will appear in the data - a dashboard
    must not quietly manufacture "Claude Code" from an absence of information."""
    assert wiring.detect_harness({}) == ("unknown", None)


def test_build_returns_none_when_telemetry_is_off(tmp_path) -> None:
    assert (
        wiring.build(
            env(tmp_path, SPEAK_TELEMETRY="off"),
            settings=Settings(),
            plugin_version="1.1.0",
            host_platform="linux",
        )
        is None
    )


def test_build_writes_an_install_row(tmp_path) -> None:
    session = wiring.build(
        env(tmp_path),
        settings=Settings(speech=VoiceSettings(prefix="SPEAK", engine="say")),
        plugin_version="1.1.0",
        host_platform="linux",
    )
    assert session is not None
    row = store.install_row(session.conn, session.install_id)
    assert row is not None
    assert row["plugin_version"] == "1.1.0"
    assert row["os"] == "linux"


def test_build_writes_a_session_row_naming_the_engines(tmp_path) -> None:
    session = wiring.build(
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


def test_build_reuses_the_install_id_and_first_seen_at_across_sessions(tmp_path) -> None:
    first = wiring.build(
        env(tmp_path), settings=Settings(), plugin_version="1.1.0", host_platform="linux"
    )
    assert first is not None
    first.conn.close()

    second = wiring.build(
        env(tmp_path), settings=Settings(), plugin_version="1.1.0", host_platform="linux"
    )
    assert second is not None
    assert second.install_id == first.install_id

    row = store.install_row(second.conn, second.install_id)
    assert row is not None
    first_row = store.install_row(
        store.connect(
            consent.config_dir(env(tmp_path), host_platform="linux") / wiring.DB_FILENAME,
            SCHEMA,
        ),
        first.install_id,
    )
    assert first_row is not None
    assert row["first_seen_at"] == first_row["first_seen_at"]


def test_each_session_gets_a_fresh_session_id(tmp_path) -> None:
    e = env(tmp_path)
    first = wiring.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    second = wiring.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    assert first is not None
    assert second is not None
    assert first.session_id != second.session_id


def test_the_machine_probe_is_cached_across_sessions(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}
    real_probe = wiring.machine.probe

    def counting_probe(**kwargs: object) -> wiring.machine.MachineFacts:
        calls["n"] += 1
        return real_probe(**kwargs)

    monkeypatch.setattr(wiring.machine, "probe", counting_probe)

    e = env(tmp_path)
    first = wiring.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    assert first is not None
    first.conn.close()
    assert calls["n"] == 1

    second = wiring.build(e, settings=Settings(), plugin_version="1.1.0", host_platform="linux")
    assert second is not None
    assert calls["n"] == 1  # reused the cached probe, did not run it again


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en_US.UTF-8", "en-US"),
        ("pt_BR", "pt-BR"),
        ("C", None),
        ("POSIX", None),
        ("C.UTF-8", None),
        ("en_US@euro", None),
        ("", None),
    ],
)
def test_language_is_a_bcp47_tag_or_nothing(lang, expected) -> None:
    """`C`/`POSIX` mean no locale, and anything outside the contract's pattern would
    have every chunk refused, because the install travels with each one."""
    assert wiring.language({"LANG": lang}) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("America/Sao_Paulo", "America/Sao_Paulo"),
        ("America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
        ("UTC", "Etc/UTC"),
        ("GMT", "Etc/GMT"),
        ("Etc/UTC", "Etc/UTC"),
        ("localtime", None),
        ("a/b/c/d", None),
        (None, None),
    ],
)
def test_timezone_is_an_iana_name_the_contract_accepts_or_nothing(raw, expected) -> None:
    assert wiring.timezone_name(raw) == expected


def test_an_overlong_engine_name_is_cut_to_the_contract_length(tmp_path) -> None:
    session = wiring.build(
        env(tmp_path),
        settings=Settings(speech=VoiceSettings(prefix="SPEAK", engine="x" * 80)),
        plugin_version="1.1.0",
        host_platform="linux",
    )
    assert session is not None
    (row,) = store.sessions_by_id(session.conn, [session.session_id])
    assert row["tts_engine"] == "x" * wiring.TTS_ENGINE_MAX_LENGTH
    assert session.tts_engine == row["tts_engine"]


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("claude-code", "claude-code"), ("Codex", "codex"), ("something-else", "unknown")],
)
def test_detect_harness_takes_a_declared_harness_only_from_the_enum(declared, expected) -> None:
    """`/talk` is no MCP session, so the host it runs under is passed in by name; a
    value outside the contract's enum would have its session refused."""
    assert wiring.detect_harness({"SPEAK_HARNESS": declared}) == (expected, None)


def test_a_declared_harness_wins_over_the_plugin_root() -> None:
    env = {"SPEAK_HARNESS": "gemini-cli", "CLAUDE_PLUGIN_ROOT": "/plugin"}
    assert wiring.detect_harness(env) == ("gemini-cli", None)
