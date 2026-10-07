"""The two console scripts: the consent command (with `notice`) and the stats report."""

import io

import pytest
from speak_core.telemetry import notice as core_notice

from voicebox.telemetry import commands
from voicebox.telemetry.contract import IDENTITY
from voicebox.telemetry.notice import TEXT


def test_notice_prints_the_text_and_marks_it_shown(tmp_path):
    env = {"HOME": str(tmp_path)}
    out = io.StringIO()
    assert commands.show_notice(env, host_platform="darwin", out=out) == 0
    assert TEXT in out.getvalue()
    assert core_notice.due(env, IDENTITY, host_platform="darwin") is False


def test_under_do_not_track_the_notice_says_nothing_is_sent_and_marks_nothing(tmp_path):
    env = {"HOME": str(tmp_path), "DO_NOT_TRACK": "1"}
    out = io.StringIO()
    commands.show_notice(env, host_platform="darwin", out=out)
    assert "sends nothing" in out.getvalue()
    assert not IDENTITY.config_dir(env, host_platform="darwin").exists()


def test_stats_without_a_store_prints_a_plain_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VOICEBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    with pytest.raises(SystemExit) as stop:
        commands.stats_main()
    assert stop.value.code == 0
    assert "No telemetry recorded yet." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda db: db.write_bytes(b"this is not a database" * 500), "moves it aside"),
        (lambda db: db.mkdir(), "cannot be read right now"),
    ],
    ids=["corrupt", "unopenable"],
)
def test_stats_on_a_store_it_cannot_read_says_so_plainly(tmp_path, monkeypatch, capsys, make, says):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VOICEBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "state").mkdir()
    make(tmp_path / "state" / "telemetry.db")
    with pytest.raises(SystemExit) as stop:
        commands.stats_main()
    assert stop.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == "" and says in printed.err
    assert str(tmp_path / "state" / "telemetry.db") in printed.err


def test_the_consent_command_saves_a_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr("sys.argv", ["voicebox-telemetry", "local"])
    with pytest.raises(SystemExit) as stop:
        commands.telemetry_main()
    assert stop.value.code == 0
    assert "voicebox telemetry is now local." in capsys.readouterr().out
