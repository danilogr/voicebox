"""The session row says which versions ran and what was on the machine."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from speak_core.telemetry import store

from voicebox.config import Settings
from voicebox.telemetry import compose


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "VOICEBOX_STATE_DIR": str(tmp_path / "state"), **extra}


def built(tmp_path: Path, *, which=lambda name: f"/usr/bin/{name}", nemo="0.2.0", **extra: str):
    env = env_for(tmp_path, **extra)
    return compose.build(
        env,
        settings=Settings.from_env(env),
        plugin_version="1.0.0",
        host_platform="darwin",
        which=which,
        nemo_version=lambda binary: nemo,
    )


def test_nothing_is_collected_when_telemetry_is_off(tmp_path):
    assert built(tmp_path, VOICEBOX_TELEMETRY="off") is None
    assert not (tmp_path / "state" / "telemetry.db").exists()


def test_the_store_is_in_the_state_dir_and_the_row_names_the_versions(tmp_path):
    session = built(tmp_path, VOICEBOX_HARNESS="antigravity")
    assert session is not None
    assert session.db_path == tmp_path / "state" / "telemetry.db"
    (row,) = store.sessions_by_id(session.conn, [session.session_id])
    assert row["plugin_version"] == "1.0.0"
    assert row["core_version"] == compose.core_version()
    assert row["nemo_version"] == "0.2.0"
    assert row["ffmpeg_present"] == 1
    assert row["harness"] == "antigravity"


def test_ffmpeg_counts_as_present_only_with_ffprobe_beside_it(tmp_path):
    session = built(tmp_path, which=lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    assert session is not None
    (row,) = store.sessions_by_id(session.conn, [session.session_id])
    assert row["ffmpeg_present"] == 0


def test_no_nemo_means_no_nemo_version(tmp_path):
    session = built(tmp_path, nemo=None)
    assert session is not None
    (row,) = store.sessions_by_id(session.conn, [session.session_id])
    assert row["nemo_version"] is None


def test_every_session_gets_a_fresh_id(tmp_path):
    first, second = built(tmp_path), built(tmp_path)
    assert first is not None and second is not None
    assert first.session_id != second.session_id


def run_printing(stdout: str, code: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    return run


def test_the_nemo_version_is_the_second_field_of_the_first_line():
    assert compose.read_nemo_version("nemo", run=run_printing("nemo-speech 0.2.0\na40bce5\n")) == (
        "0.2.0"
    )


def test_a_version_that_is_not_semver_is_omitted():
    assert compose.read_nemo_version("nemo", run=run_printing("nemo-speech dev-build\n")) is None
    assert compose.read_nemo_version("nemo", run=run_printing("")) is None
    assert compose.read_nemo_version("nemo", run=run_printing("nemo-speech 0.2.0", 1)) is None


def test_a_missing_or_hanging_binary_is_no_version():
    def missing(argv: list[str], **kwargs: object) -> NoReturn:
        raise FileNotFoundError(argv[0])

    def hangs(argv: list[str], **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(argv, 2)

    assert compose.read_nemo_version("nope", run=missing) is None
    assert compose.read_nemo_version("slow", run=hangs) is None


def test_core_version_is_semver():
    assert compose.semver_or_none(compose.core_version()) == compose.core_version()
    assert compose.semver_or_none("1.2") is None
    assert compose.semver_or_none("1.2.3-rc.1") == "1.2.3-rc.1"
    assert compose.semver_or_none("1.2.3\n") == "1.2.3"


def test_only_release_versions_ship(monkeypatch):
    monkeypatch.setattr(compose, "core_version", lambda: "0.3.0")
    assert compose.versions_shippable("1.0.0") is True
    assert compose.versions_shippable("1.0.0-dev") is False
    monkeypatch.setattr(compose, "core_version", lambda: "0.3.0+local")
    assert compose.versions_shippable("1.0.0") is False


def test_this_release_ships():
    """Release-time check: the plugin's and the pinned core's versions are the contract's
    release versions, so a tagged build is one that sends."""
    from voicebox.server import VERSION

    assert compose.versions_shippable(VERSION)


def test_a_corrupt_store_collects_nothing_and_does_not_raise(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "telemetry.db").write_bytes(b"this is not a database" * 500)
    assert built(tmp_path) is None


def test_non_utf8_output_from_the_binary_is_no_version():
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    assert compose.read_nemo_version("nemo", run=run) is None
    assert compose.read_nemo_version("nemo", run=run_printing("nemo-speech 0.2.0\ufffd\n")) is None
