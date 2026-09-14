"""The §1.2 hardware probe: bounded, cached, and honest about what it could not tell."""

import time

from speak.telemetry import machine


def test_probe_never_calls_system_profiler(monkeypatch) -> None:
    """The one disqualifying source (spec 1.2): it takes whole seconds, which this
    plugin's first rule - never block or delay speech - cannot afford."""
    import subprocess

    real_run = subprocess.run

    def guarded(args: list[str], *a: object, **kw: object) -> object:
        assert "system_profiler" not in args
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded)
    machine.probe(host_platform="darwin")
    machine.probe(host_platform="linux")


def test_a_failing_probe_yields_unknown_rather_than_raising() -> None:
    def boom() -> str:
        raise OSError("no such binary")

    assert machine._timed(boom) == machine.UNKNOWN


def test_a_slow_probe_yields_unknown() -> None:
    def slow() -> str:
        time.sleep(0.01)
        return "value"

    assert machine._timed(slow, budget_ms=1.0) == machine.UNKNOWN


def test_a_fast_probe_keeps_its_value() -> None:
    assert machine._timed(lambda: "Apple M5") == "Apple M5"


def test_an_empty_result_is_treated_as_unknown() -> None:
    assert machine._timed(lambda: "  ") == machine.UNKNOWN


def test_arch_normalises_common_spellings() -> None:
    assert machine._ARCH_MAP["aarch64"] == "arm64"
    assert machine._ARCH_MAP["amd64"] == "x86_64"


def test_os_version_is_major_minor_only() -> None:
    version = machine._os_version("darwin")
    assert version == machine.UNKNOWN or len(version.split(".")) == 2


def test_ram_is_bucketed_to_the_nearest_named_size() -> None:
    assert machine._bucket_ram(24 * 1024**3) == 32
    assert machine._bucket_ram(18 * 1024**3) == 16
    assert machine._bucket_ram(96 * 1024**3) == 128
    assert machine._bucket_ram(8 * 1024**3) == 8


def test_accel_is_metal_only_on_apple_silicon() -> None:
    assert machine._accel("darwin", "arm64", "Apple M5") == ("metal", "Apple M5")
    assert machine._accel("darwin", "x86_64", "Intel Core i7") == (machine.UNKNOWN, machine.UNKNOWN)


def test_linux_gpu_signal_is_none_when_neither_marker_exists(tmp_path) -> None:
    signal = machine._linux_gpu_signal(tmp_path / "no-nvidia", tmp_path / "no-amdgpu")
    assert signal == ("none", machine.UNKNOWN)


def test_linux_gpu_signal_is_cuda_when_the_nvidia_marker_exists(tmp_path) -> None:
    nvidia = tmp_path / "nvidia-version"
    nvidia.write_text("NVRM version: 550.00\n")
    signal = machine._linux_gpu_signal(nvidia, tmp_path / "no-amdgpu")
    assert signal == ("cuda", machine.UNKNOWN)


def test_linux_gpu_signal_is_rocm_when_the_amdgpu_module_directory_exists(tmp_path) -> None:
    amdgpu = tmp_path / "amdgpu"
    amdgpu.mkdir()
    signal = machine._linux_gpu_signal(tmp_path / "no-nvidia", amdgpu)
    assert signal == ("rocm", machine.UNKNOWN)


def test_probe_never_raises_and_fills_every_field() -> None:
    facts = machine.probe(host_platform="darwin")
    assert facts.os == "darwin"
    assert facts.arch in ("arm64", "x86_64", machine.UNKNOWN, "")  # never raises either way
    assert isinstance(facts.cpu_model, str)


def test_ensure_current_reuses_the_cache_when_nothing_moved() -> None:
    cached = machine.MachineFacts(
        os="darwin",
        os_version=machine._os_version("darwin"),  # must match this real machine's own
        arch="arm64",
        cpu_model="Apple M5",
        cpu_count=12,
        ram_gb=32,
        accel="metal",
        accel_name="Apple M5",
    )
    reused = machine.ensure_current(
        cached, cached_plugin_version="1.1.0", plugin_version="1.1.0", host_platform="darwin"
    )
    assert reused is cached


def test_ensure_current_reprobes_when_the_plugin_version_changed(monkeypatch) -> None:
    cached = machine.MachineFacts(
        os="darwin",
        os_version="15.4",
        arch="arm64",
        cpu_model="Apple M5",
        cpu_count=12,
        ram_gb=32,
        accel="metal",
        accel_name="Apple M5",
    )
    monkeypatch.setattr(machine, "_os_version", lambda host_platform: "15.4")
    fresh = machine.ensure_current(
        cached, cached_plugin_version="1.0.0", plugin_version="1.1.0", host_platform="darwin"
    )
    assert fresh is not cached
    assert fresh.os_version == "15.4"


def test_ensure_current_reprobes_when_the_os_version_changed() -> None:
    cached = machine.MachineFacts(
        os="darwin",
        os_version="14.0",
        arch="arm64",
        cpu_model="Apple M5",
        cpu_count=12,
        ram_gb=32,
        accel="metal",
        accel_name="Apple M5",
    )
    fresh = machine.ensure_current(
        cached, cached_plugin_version="1.1.0", plugin_version="1.1.0", host_platform="darwin"
    )
    assert fresh is not cached


def test_to_mapping_and_from_mapping_round_trip() -> None:
    facts = machine.MachineFacts(
        os="linux",
        os_version="6.8",
        arch="x86_64",
        cpu_model="unknown",
        cpu_count=None,
        ram_gb=None,
        accel="none",
        accel_name="unknown",
    )
    assert machine.MachineFacts.from_mapping(facts.to_mapping()) == facts
