"""A plugin's telemetry identity: its variables, its directory, its endpoint - and
nothing of another plugin's."""

from pathlib import Path

from telemetry_fixtures import IDENTITY

from speak.telemetry import consent
from speak.telemetry.identity import TelemetryIdentity

OTHER = TelemetryIdentity(
    plugin="voicebox",
    env_prefix="VOICEBOX",
    default_endpoint="https://voicebox.example/v1/ingest",
    disclosure_version=1,
)


def test_variable_names_derive_from_the_prefix():
    assert IDENTITY.mode_env == "SPEAK_TELEMETRY"
    assert IDENTITY.debug_env == "SPEAK_TELEMETRY_DEBUG"
    assert IDENTITY.endpoint_env == "SPEAK_TELEMETRY_ENDPOINT"
    assert IDENTITY.harness_env == "SPEAK_HARNESS"


def test_config_dir_is_application_support_on_darwin_never_a_cache():
    path = IDENTITY.config_dir(
        {"HOME": "/Users/x", "SPEAK_STATE_DIR": "/cache/x"}, host_platform="darwin"
    )
    assert path == Path("/Users/x/Library/Application Support/speak")


def test_config_dir_follows_xdg_config_home_elsewhere():
    assert IDENTITY.config_dir({"XDG_CONFIG_HOME": "/config"}, host_platform="linux") == Path(
        "/config/speak"
    )


def test_config_dir_falls_back_to_dot_config():
    assert IDENTITY.config_dir({"HOME": "/home/x"}, host_platform="linux") == Path(
        "/home/x/.config/speak"
    )


def test_each_plugin_has_its_own_config_dir():
    env = {"HOME": "/home/x"}
    assert IDENTITY.config_dir(env, host_platform="linux") != OTHER.config_dir(
        env, host_platform="linux"
    )


def test_the_endpoint_is_the_default_unless_this_plugins_variable_says_otherwise():
    assert IDENTITY.ingest_url({}) == "https://telemetry.example/v1/ingest"
    assert IDENTITY.ingest_url({"SPEAK_TELEMETRY_ENDPOINT": "https://x/v1/ingest"}) == (
        "https://x/v1/ingest"
    )


def test_another_plugins_endpoint_does_not_move_this_one():
    assert IDENTITY.ingest_url({"VOICEBOX_TELEMETRY_ENDPOINT": "https://x/v1/ingest"}) == (
        "https://telemetry.example/v1/ingest"
    )


def test_another_plugins_debug_switch_does_not_turn_on_this_ones():
    assert IDENTITY.debug_requested({"SPEAK_TELEMETRY_DEBUG": "1"}) is True
    assert IDENTITY.debug_requested({"VOICEBOX_TELEMETRY_DEBUG": "1"}) is False
    assert IDENTITY.debug_requested({"SPEAK_TELEMETRY_DEBUG": "0"}) is False


def test_another_plugins_off_switch_does_not_turn_this_one_off(tmp_path):
    consented = consent.load_consent(
        {"HOME": str(tmp_path), "VOICEBOX_TELEMETRY": "off"}, IDENTITY, host_platform="linux"
    )
    assert consented.mode == consent.DEFAULT_MODE
