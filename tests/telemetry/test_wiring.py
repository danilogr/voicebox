"""What every plugin's telemetry composition shares: harness and locale detection and
the install row brought up to date."""

import pytest
from telemetry_fixtures import IDENTITY, UTTERANCES

from speak.telemetry import store, wiring


def start(tmp_path, **extra: str):
    return wiring.start_install(
        {"HOME": str(tmp_path), **extra},
        IDENTITY,
        UTTERANCES,
        db_path=tmp_path / "telemetry.db",
        plugin_version="1.1.0",
        host_platform="linux",
    )


def test_detect_harness_reads_claude_plugin_root() -> None:
    assert wiring.detect_harness({"CLAUDE_PLUGIN_ROOT": "/some/path"}, IDENTITY) == (
        "claude-code",
        None,
    )


def test_detect_harness_is_honestly_unknown_without_a_signal() -> None:
    """spec 2: "unknown" is a real value that will appear in the data - a dashboard
    must not quietly manufacture "Claude Code" from an absence of information."""
    assert wiring.detect_harness({}, IDENTITY) == ("unknown", None)


def test_start_install_returns_none_when_telemetry_is_off(tmp_path) -> None:
    assert start(tmp_path, SPEAK_TELEMETRY="off") is None


def test_start_install_writes_the_install_row(tmp_path) -> None:
    install = start(tmp_path)
    assert install is not None
    row = store.install_row(install.conn, install.consent.install_id)
    assert row is not None
    assert row["plugin_version"] == "1.1.0"
    assert row["os"] == "linux"


def test_the_install_id_and_first_seen_at_survive_across_sessions(tmp_path) -> None:
    first = start(tmp_path)
    assert first is not None
    first_row = store.install_row(first.conn, first.consent.install_id)
    first.conn.close()
    second = start(tmp_path)
    assert second is not None
    assert second.consent.install_id == first.consent.install_id
    row = store.install_row(second.conn, second.consent.install_id)
    assert row is not None and first_row is not None
    assert row["first_seen_at"] == first_row["first_seen_at"]


def test_the_machine_probe_is_cached_across_sessions(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}
    real_probe = wiring.machine.probe

    def counting_probe(**kwargs: object) -> wiring.machine.MachineFacts:
        calls["n"] += 1
        return real_probe(**kwargs)

    monkeypatch.setattr(wiring.machine, "probe", counting_probe)
    first = start(tmp_path)
    assert first is not None
    first.conn.close()
    second = start(tmp_path)
    assert second is not None
    assert calls["n"] == 1


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


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("claude-code", "claude-code"), ("Codex", "codex"), ("something-else", "unknown")],
)
def test_detect_harness_takes_a_declared_harness_only_from_the_enum(declared, expected) -> None:
    """`/talk` is no MCP session, so the host it runs under is passed in by name; a
    value outside the contract's enum would have its session refused."""
    assert wiring.detect_harness({"SPEAK_HARNESS": declared}, IDENTITY) == (expected, None)


def test_a_declared_harness_wins_over_the_plugin_root() -> None:
    env = {"SPEAK_HARNESS": "gemini-cli", "CLAUDE_PLUGIN_ROOT": "/plugin"}
    assert wiring.detect_harness(env, IDENTITY) == ("gemini-cli", None)
