"""Composition: consent, the machine probe, the local store, and the session row
built from them - what both `__main__` and `talk` hand their `TelemetrySession` from.
"""

from speak.config import Settings
from speak.telemetry import consent, store, wiring


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
        settings=Settings(engine="say"),
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
        settings=Settings(engine="nemo", nemo_model="magpie", nemo_asr_model="parakeet"),
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
            consent.config_dir(env(tmp_path), host_platform="linux") / wiring.DB_FILENAME
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
