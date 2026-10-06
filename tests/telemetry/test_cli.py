"""The consent command each plugin registers as its own `<plugin>-telemetry`."""

import io

from telemetry_fixtures import IDENTITY

from speak.telemetry import cli, consent


def run(tmp_path, *argv: str, allow_record: bool = False, env: dict[str, str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        IDENTITY,
        list(argv),
        env if env is not None else {"HOME": str(tmp_path)},
        host_platform="linux",
        allow_record=allow_record,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def stored(tmp_path) -> consent.Consent | None:
    path = consent.consent_path({"HOME": str(tmp_path)}, IDENTITY, host_platform="linux")
    return consent.ConsentStore(path, disclosure_version=IDENTITY.disclosure_version).load()


def test_each_mode_is_saved(tmp_path):
    for mode in consent.MODES:
        code, out, _ = run(tmp_path, mode)
        assert code == 0
        assert f"speak telemetry is now {mode}" in out
        saved = stored(tmp_path)
        assert saved is not None
        assert saved.mode == mode


def test_switching_mode_keeps_the_install_id(tmp_path):
    run(tmp_path, "off")
    first = stored(tmp_path)
    run(tmp_path, "on")
    second = stored(tmp_path)
    assert first is not None and second is not None
    assert first.install_id and first.install_id == second.install_id


def test_off_says_the_install_ping_still_goes(tmp_path):
    _, out, _ = run(tmp_path, "off")
    assert "DO_NOT_TRACK=1" in out


def test_id_prints_the_install_id(tmp_path):
    run(tmp_path, "on")
    code, out, _ = run(tmp_path, "--id")
    saved = stored(tmp_path)
    assert code == 0
    assert saved is not None
    assert out.strip() == saved.install_id


def test_id_without_an_install_says_so(tmp_path):
    code, _, err = run(tmp_path, "--id")
    assert code == 1
    assert "no install id yet" in err


def test_status_reports_the_saved_mode(tmp_path):
    run(tmp_path, "local")
    code, out, _ = run(tmp_path)
    assert code == 0
    assert "speak telemetry: local" in out


def test_status_says_when_the_environment_overrides_the_saved_mode(tmp_path):
    run(tmp_path, "on")
    _, out, _ = run(tmp_path, env={"HOME": str(tmp_path), "DO_NOT_TRACK": "1"})
    assert "overridden to off" in out


def test_record_is_refused_unless_the_plugin_allows_it(tmp_path):
    code, _, err = run(tmp_path, "record", "on")
    assert code == 2
    assert "usage: speak-telemetry" in err
    assert "record" not in err


def test_record_is_saved_when_the_plugin_allows_it(tmp_path):
    run(tmp_path, "record", "on", allow_record=True)
    saved = stored(tmp_path)
    assert saved is not None and saved.record is True
    run(tmp_path, "record", "off", allow_record=True)
    saved = stored(tmp_path)
    assert saved is not None and saved.record is False


def test_an_unknown_argument_prints_the_usage(tmp_path):
    code, _, err = run(tmp_path, "sometimes")
    assert code == 2
    assert "usage: speak-telemetry [on|local|off|--id]" in err


def test_a_config_directory_that_cannot_be_written_is_reported(tmp_path):
    home = tmp_path / "home"
    home.write_text("a file where the home directory should be")
    code, _, err = run(tmp_path, "off", env={"HOME": str(home)})
    assert code == 1
    assert "could not save" in err
