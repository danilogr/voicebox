"""The §1.2 hardware probe: platform, CPU, RAM, and the acceleration backend.

Gathering these must never be slow: every source here is `platform`, `os.cpu_count`,
a `sysctl` call on macOS, or a `/proc` read on Linux - never `system_profiler`, which
costs whole seconds. A probe that fails or runs past `PROBE_BUDGET_MS` yields
"unknown" for that one field rather than raising; "unknown" is a real value that has
to survive into the uploaded data.

`ram_gb` is bucketed and `os_version` is major.minor only, because together with
`cpu_model` and `accel_name` these fields are more identifying in combination than
any one of them alone.
"""

import logging
import math
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"
PROBE_BUDGET_MS = 200.0
RAM_BUCKETS = (8, 16, 32, 64, 128)

_ARCH_MAP = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
}


@dataclass(frozen=True, slots=True)
class MachineFacts:
    """The columns spec 1.2 names. A string field that could not be answered holds
    `UNKNOWN`; a numeric one holds `None` - there is no numeric "unknown"."""

    os: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_count: int | None
    ram_gb: int | None
    accel: str
    accel_name: str

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "MachineFacts":
        return cls(
            os=data["os"],
            os_version=data["os_version"],
            arch=data["arch"],
            cpu_model=data["cpu_model"],
            cpu_count=data["cpu_count"],
            ram_gb=data["ram_gb"],
            accel=data["accel"],
            accel_name=data["accel_name"],
        )


def _timed(fn: Callable[[], str], *, budget_ms: float = PROBE_BUDGET_MS) -> str:
    """Run one probe, returning `UNKNOWN` on any failure or overrun."""
    started = time.monotonic()
    try:
        value = fn()
    except Exception as error:  # a missing binary, a permissions error, a parse failure
        logger.debug("machine probe failed: %s", error)
        return UNKNOWN
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > budget_ms:
        logger.debug("machine probe took %.0fms, past the %.0fms budget", elapsed_ms, budget_ms)
        return UNKNOWN
    text = (value or "").strip()
    return text or UNKNOWN


def _sysctl(name: str) -> str:
    result = subprocess.run(
        ["sysctl", "-n", name],
        capture_output=True,
        text=True,
        timeout=PROBE_BUDGET_MS / 1000,
        check=True,
    )
    return result.stdout.strip()


def _linux_cpu_model() -> str:
    with Path("/proc/cpuinfo").open(encoding="utf-8") as handle:
        for line in handle:
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    return ""


def _cpu_model(host_platform: str) -> str:
    if host_platform == "darwin":
        return _timed(lambda: _sysctl("machdep.cpu.brand_string"))
    if host_platform == "linux":
        return _timed(_linux_cpu_model)
    return _timed(platform.processor)


def _arch() -> str:
    raw = platform.machine().lower()
    return _ARCH_MAP.get(raw, raw or UNKNOWN)


def _os_version(host_platform: str) -> str:
    """Major.minor only, from a plain `platform` call - never a subprocess."""
    if host_platform == "darwin":
        raw = platform.mac_ver()[0]
    elif host_platform == "linux":
        raw = platform.release()
    else:
        raw = platform.version()
    match = re.match(r"(\d+)\.(\d+)", raw or "")
    return f"{match.group(1)}.{match.group(2)}" if match else UNKNOWN


def _linux_ram_bytes() -> int:
    with Path("/proc/meminfo").open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    raise ValueError("MemTotal not found in /proc/meminfo")


def _ram_bytes(host_platform: str) -> int | None:
    if host_platform == "darwin":
        return int(_sysctl("hw.memsize"))
    if host_platform == "linux":
        return _linux_ram_bytes()
    return None  # no cheap stdlib source on this platform


def _bucket_ram(total_bytes: int) -> int:
    """The bucket whose size is geometrically closest to the real total.

    The buckets double each step, so nearness is judged on a log scale: 24 GB sits
    equally far from 16 and 32 in absolute terms but is closer to 32 on this scale,
    which is how the real number would be described in conversation.
    """
    gb = max(total_bytes / (1024**3), 1.0)
    return min(RAM_BUCKETS, key=lambda bucket: abs(math.log2(gb) - math.log2(bucket)))


def _ram_gb(host_platform: str) -> int | None:
    started = time.monotonic()
    try:
        total = _ram_bytes(host_platform)
    except Exception as error:
        logger.debug("RAM probe failed: %s", error)
        return None
    if total is None:
        return None
    if (time.monotonic() - started) * 1000 > PROBE_BUDGET_MS:
        logger.debug("RAM probe took too long")
        return None
    return _bucket_ram(total)


def _accel(host_platform: str, arch: str, cpu_model: str) -> tuple[str, str]:
    """(accel, accel_name).

    Metal is certain only on Apple Silicon, where the GPU is part of the SoC named
    by `cpu_model`. Every other case needs a signal this module has no cheap way to
    get, so it reports what a file's presence can prove and "unknown" for the rest -
    never a guess dressed up as a measurement.
    """
    if host_platform == "darwin" and arch == "arm64":
        return "metal", cpu_model
    if host_platform == "linux":
        return _linux_gpu_signal(Path("/proc/driver/nvidia/version"), Path("/sys/module/amdgpu"))
    return UNKNOWN, UNKNOWN


def _linux_gpu_signal(nvidia_marker: Path, amdgpu_marker: Path) -> tuple[str, str]:
    """Pure over its two paths, so a test can point them at a `tmp_path` fixture
    instead of depending on whether this machine happens to have a GPU driver."""
    if nvidia_marker.exists():
        return "cuda", UNKNOWN
    if amdgpu_marker.is_dir():
        return "rocm", UNKNOWN
    return "none", UNKNOWN


def probe(*, host_platform: str | None = None) -> MachineFacts:
    """A fresh probe of every field, each individually bounded."""
    host_platform = host_platform if host_platform is not None else sys.platform
    arch = _arch()
    cpu_model = _cpu_model(host_platform)
    accel, accel_name = _accel(host_platform, arch, cpu_model)
    return MachineFacts(
        os=host_platform,
        os_version=_os_version(host_platform),
        arch=arch,
        cpu_model=cpu_model,
        cpu_count=os.cpu_count(),
        ram_gb=_ram_gb(host_platform),
        accel=accel,
        accel_name=accel_name,
    )


def ensure_current(
    cached: MachineFacts | None,
    *,
    cached_plugin_version: str | None,
    plugin_version: str,
    host_platform: str | None = None,
) -> MachineFacts:
    """The cached facts, unless the plugin or the OS has moved on since they were taken.

    These are static facts about the machine: re-probing every session buys nothing,
    and probing at all has to stay rare, not merely individually cheap.
    """
    host_platform = host_platform if host_platform is not None else sys.platform
    if (
        cached is not None
        and cached_plugin_version == plugin_version
        and cached.os_version == _os_version(host_platform)
    ):
        return cached
    return probe(host_platform=host_platform)
