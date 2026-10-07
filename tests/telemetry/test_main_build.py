"""`__main__.build()` decides whether a session collects, pings and ships."""

import sys
from pathlib import Path

import pytest
from speak_core.telemetry import notice

from voicebox import __main__ as entry
from voicebox.config import Settings
from voicebox.telemetry import compose
from voicebox.telemetry.contract import IDENTITY


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "VOICEBOX_STATE_DIR": str(tmp_path / "state"),
        "SPEAK_CORE_STATE_DIR": str(tmp_path / "core"),
        "VOICEBOX_ENGINE": "command",
        "VOICEBOX_COMMAND": "true",
        "NEMO_AUTOSTART": "0",
        **extra,
    }


def mark_notice_shown(env: dict[str, str]) -> None:

    assert notice.mark_shown(env, IDENTITY, host_platform=sys.platform)


@pytest.fixture
def captured(monkeypatch):
    """What `build` hands the server, and the background tasks it would start."""
    spawned: list[str] = []
    served: dict[str, object] = {}

    def spawn(coroutine) -> None:
        spawned.append(coroutine.cr_code.co_name)
        coroutine.close()

    def create_server(**kwargs: object) -> object:
        served.update(kwargs)
        return object()

    monkeypatch.setattr(entry, "_spawn", spawn)
    monkeypatch.setattr(entry, "create_server", create_server)
    return spawned, served


def run_build(captured, env: dict[str, str]):
    spawned, served = captured
    entry.build(Settings.from_env(env), env)
    served["on_session_start"]()  # type: ignore[operator]
    return spawned, served


def test_a_release_build_after_the_notice_pings_and_ships(tmp_path, captured):
    env = env_for(tmp_path, DO_NOT_TRACK="")
    mark_notice_shown(env)
    spawned, served = run_build(captured, env)
    assert spawned == ["_send_ping", "_ship_backlog"]
    assert served["telemetry"] is not None


def test_nothing_is_collected_pinged_or_shipped_while_the_notice_is_due(tmp_path, captured):
    env = env_for(tmp_path, DO_NOT_TRACK="")
    spawned, served = run_build(captured, env)
    assert spawned == []
    assert served["telemetry"] is None
    assert not (tmp_path / "state" / "telemetry.db").exists()


def test_nothing_is_collected_pinged_or_shipped_under_do_not_track(tmp_path, captured):
    spawned, served = run_build(captured, env_for(tmp_path, DO_NOT_TRACK="1"))
    assert spawned == []
    assert served["telemetry"] is None


def test_a_build_that_is_not_a_release_collects_but_never_sends(tmp_path, captured, monkeypatch):
    monkeypatch.setattr(compose, "versions_shippable", lambda version: False)
    env = env_for(tmp_path, DO_NOT_TRACK="")
    mark_notice_shown(env)
    spawned, served = run_build(captured, env)
    assert spawned == []
    assert served["telemetry"] is not None


def test_a_store_that_cannot_be_opened_does_not_stop_the_server_from_being_built(
    tmp_path, captured
):
    env = env_for(tmp_path, DO_NOT_TRACK="")
    mark_notice_shown(env)
    (tmp_path / "state" / "telemetry.db").mkdir(parents=True)
    spawned, served = run_build(captured, env)
    assert served["telemetry"] is None
    assert "_ship_backlog" not in spawned


def test_a_sweep_that_cannot_run_is_logged_as_a_warning_and_the_server_is_still_built(
    tmp_path, captured, monkeypatch, caplog
):
    def sweep(desk: object) -> None:
        raise OSError("state dir unreadable")

    monkeypatch.setattr(entry, "sweep", sweep)
    env = env_for(tmp_path, DO_NOT_TRACK="1")
    with caplog.at_level("WARNING", logger=entry.logger.name):
        run_build(captured, env)
    assert [r.levelname for r in caplog.records if "earlier jobs" in r.getMessage()] == ["WARNING"]


def test_narration_gets_no_telemetry_while_the_notice_is_due(tmp_path, captured):
    env = env_for(tmp_path, DO_NOT_TRACK="")
    _, served = run_build(captured, env)
    narration = served["narration"]
    assert narration._telemetry is None  # type: ignore[attr-defined]


def test_narration_gets_the_session_once_the_notice_is_shown(tmp_path, captured):
    env = env_for(tmp_path, DO_NOT_TRACK="")
    mark_notice_shown(env)
    _, served = run_build(captured, env)
    assert served["narration"]._telemetry is served["telemetry"]  # type: ignore[attr-defined]


def test_the_model_and_the_probe_belong_to_the_nemo_engine_only(tmp_path, captured):
    _, served = run_build(captured, env_for(tmp_path, DO_NOT_TRACK="1"))
    narration = served["narration"]
    assert (narration._engine, narration._model, narration._warm) == (  # type: ignore[attr-defined]
        "command",
        None,
        None,
    )
    run_build(
        captured,
        {**env_for(tmp_path, DO_NOT_TRACK="1"), "VOICEBOX_ENGINE": "nemo"},
    )
    narration = served["narration"]
    assert narration._engine == "nemo" and narration._model  # type: ignore[attr-defined]
    assert narration._warm is not None  # type: ignore[attr-defined]
