"""Consent: the switches, the persisted install identity, and where both live."""

from telemetry_fixtures import IDENTITY

from speak.telemetry import consent
from speak.telemetry.machine import MachineFacts

FACTS = MachineFacts(
    os="darwin",
    os_version="15.4",
    arch="arm64",
    cpu_model="Apple M5",
    cpu_count=12,
    ram_gb=32,
    accel="metal",
    accel_name="Apple M5",
)


def test_a_fresh_environment_gets_a_new_install_id_and_the_default_mode(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    consented = consent.load_consent(env, IDENTITY, host_platform="linux")
    assert consented.install_id
    assert consented.mode == consent.DEFAULT_MODE
    # Opt-out: a fresh install transmits. Pinned literally, not via the constant,
    # so changing the default has to be a deliberate edit to a test that says why.
    assert consented.mode == "on"


def test_the_install_id_is_a_real_uuid4_never_hardware_derived(tmp_path) -> None:
    import uuid

    env = {"HOME": str(tmp_path)}
    consented = consent.load_consent(env, IDENTITY, host_platform="linux")
    parsed = uuid.UUID(consented.install_id)
    assert parsed.version == 4


def test_a_second_load_reuses_the_same_install_id(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    first = consent.load_consent(env, IDENTITY, host_platform="linux")
    second = consent.load_consent(env, IDENTITY, host_platform="linux")
    assert first.install_id == second.install_id


def test_speak_telemetry_off_forces_the_mode_off(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "SPEAK_TELEMETRY": "off"}
    consented = consent.load_consent(env, IDENTITY, host_platform="linux")
    assert consented.mode == "off"
    assert consented.collects is False


def test_speak_telemetry_off_never_touches_the_filesystem(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "SPEAK_TELEMETRY": "off"}
    consent.load_consent(env, IDENTITY, host_platform="linux")
    assert not (tmp_path / ".config" / "speak").exists()


def test_do_not_track_forces_the_mode_off(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "DO_NOT_TRACK": "1"}
    assert consent.load_consent(env, IDENTITY, host_platform="linux").mode == "off"


def test_do_not_track_zero_does_not_force_anything_off(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "DO_NOT_TRACK": "0"}
    assert consent.load_consent(env, IDENTITY, host_platform="linux").mode != "off"


def test_an_environment_override_cannot_turn_telemetry_on_over_a_config_file(tmp_path) -> None:
    """A CI runner's stray SPEAK_TELEMETRY=on must not be able to enable collection
    a developer's own machine has switched off in the persisted config."""
    env = {"HOME": str(tmp_path)}
    store = consent.ConsentStore(
        IDENTITY.config_dir(env, host_platform="linux") / consent.CONFIG_FILENAME,
        disclosure_version=2,
    )
    store.save(consent.Consent(install_id="fixed-id", disclosure_ver=2, mode="off"))

    loaded = consent.load_consent({**env, "SPEAK_TELEMETRY": "on"}, IDENTITY, host_platform="linux")
    assert loaded.mode == "off"


def test_a_corrupt_config_file_degrades_to_starting_over(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    path = IDENTITY.config_dir(env, host_platform="linux") / consent.CONFIG_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("not json")

    consented = consent.load_consent(env, IDENTITY, host_platform="linux")
    assert consented.install_id  # started over rather than raising


def test_save_machine_persists_the_probe_and_its_cache_key(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    consented = consent.load_consent(env, IDENTITY, host_platform="linux")

    updated = consent.save_machine(
        env, IDENTITY, consented, host_platform="linux", facts=FACTS, plugin_version="1.1.0"
    )
    assert updated.machine == FACTS
    assert updated.machine_plugin_version == "1.1.0"

    reloaded = consent.load_consent(env, IDENTITY, host_platform="linux")
    assert reloaded.machine == FACTS
    assert reloaded.machine_plugin_version == "1.1.0"


def test_save_machine_does_not_write_when_telemetry_is_off(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    off = consent.Consent(install_id="", disclosure_ver=2, mode="off")
    consent.save_machine(
        env, IDENTITY, off, host_platform="linux", facts=FACTS, plugin_version="1.1.0"
    )
    assert not (IDENTITY.config_dir(env, host_platform="linux") / consent.CONFIG_FILENAME).exists()


def test_uploads_is_true_only_in_on_mode() -> None:
    assert consent.Consent(install_id="x", disclosure_ver=2, mode="off").uploads is False
    assert consent.Consent(install_id="x", disclosure_ver=2, mode="local").uploads is False
    assert consent.Consent(install_id="x", disclosure_ver=2, mode="on").uploads is True


def test_an_unrecognised_stored_mode_falls_back_to_the_default() -> None:
    loaded = consent.Consent.from_mapping(
        {"install_id": "x", "mode": "nonsense"}, disclosure_version=2
    )
    assert loaded.mode == consent.DEFAULT_MODE
