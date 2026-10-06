"""Telemetry consent: the switches, the persisted install identity, and where both live.

Consent lives in the OS's per-user config directory, never `SPEAK_STATE_DIR` - that
is a cache directory the OS may clear, which would silently re-enable telemetry
someone had switched off.

Like `settings.Settings`, this module takes the environment as a plain mapping rather
than reading `os.environ` itself, so it stays testable without patching globals; the
real read happens once, in `__main__` or `talk`.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from speak.telemetry.machine import MachineFacts

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_LOCAL = "local"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_LOCAL, MODE_ON)

# Opt-out: a new install collects and transmits. Anonymous counts and timings
# only - never what was said. `off` and `local` are one command away, and
# `DO_NOT_TRACK=1` is honoured without any config at all.
#
# Off by default was considered and rejected: opt-in rates skew heavily toward
# power users, which would bias the one measurement this exists for - telling a
# few heavy users apart from many light ones.
DEFAULT_MODE = MODE_ON

# Which version of TELEMETRY.md describes what this install sends; 2 is the first
# that includes the install ping.
DISCLOSURE_VERSION = 2
CONFIG_FILENAME = "telemetry.json"


def config_dir(env: Mapping[str, str], *, host_platform: str) -> Path:
    """Where consent and the cached machine probe live.

    Not `SPEAK_STATE_DIR`: that is a cache directory the OS may clear, which would
    silently re-enable telemetry someone had switched off.
    """
    home = Path(env.get("HOME", "~")).expanduser()
    if host_platform == "darwin":
        return home / "Library" / "Application Support" / "speak"
    base = env.get("XDG_CONFIG_HOME")
    return (Path(base) if base else home / ".config") / "speak"


@dataclass(frozen=True, slots=True)
class Consent:
    """What this install has agreed to, and its identity.

    Timestamps (`first_seen_at`, `last_seen_at`) are not carried here: they belong
    to the uploadable `installs` row in the local store, which is the one place
    they need to live.
    """

    install_id: str
    mode: str = DEFAULT_MODE
    record: bool = False
    disclosure_ver: int = DISCLOSURE_VERSION
    machine: MachineFacts | None = None
    machine_plugin_version: str | None = None

    @property
    def collects(self) -> bool:
        return self.mode != MODE_OFF

    @property
    def uploads(self) -> bool:
        return self.mode == MODE_ON

    def to_mapping(self) -> dict[str, Any]:
        return {
            "install_id": self.install_id,
            "mode": self.mode,
            "record": self.record,
            "disclosure_ver": self.disclosure_ver,
            "machine": self.machine.to_mapping() if self.machine is not None else None,
            "machine_plugin_version": self.machine_plugin_version,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Consent":
        machine_data = data.get("machine")
        mode = data.get("mode")
        return cls(
            install_id=str(data["install_id"]),
            mode=mode if mode in MODES else DEFAULT_MODE,
            record=bool(data.get("record", False)),
            disclosure_ver=int(data.get("disclosure_ver", DISCLOSURE_VERSION)),
            machine=MachineFacts.from_mapping(machine_data) if machine_data else None,
            machine_plugin_version=data.get("machine_plugin_version"),
        )


class ConsentStore:
    """Reads and writes the config file. A missing or unreadable file is `None`,
    never an exception - a corrupt config must degrade to "start over", not crash
    the plugin."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Consent | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return Consent.from_mapping(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            logger.debug("telemetry config at %s is unreadable: %s", self._path, error)
            return None

    def save(self, consent: Consent) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(consent.to_mapping(), indent=2), encoding="utf-8")
        except OSError as error:
            logger.debug("could not write telemetry config at %s: %s", self._path, error)


def do_not_track(env: Mapping[str, str]) -> bool:
    """`DO_NOT_TRACK` set to anything but empty or `0`: nothing is sent at all, not
    even the install ping, which every other setting still sends."""
    return (env.get("DO_NOT_TRACK") or "").strip() not in ("", "0")


def _forced_off(env: Mapping[str, str]) -> bool:
    """`SPEAK_TELEMETRY=off` and `DO_NOT_TRACK=1` force collection off for this run.

    Neither can turn telemetry *on* over a config file that says otherwise - an
    environment variable leaking into a shared runner must not be able to enable
    collection nobody configured on that machine.
    """
    if (env.get("SPEAK_TELEMETRY") or "").strip().lower() == MODE_OFF:
        return True
    return do_not_track(env)


def load_consent(env: Mapping[str, str], *, host_platform: str) -> Consent:
    """The effective consent for this process.

    A forced-off environment skips the filesystem entirely: it must not create a
    config file, let alone an install id, for a machine that just said no.
    """
    if _forced_off(env):
        return Consent(install_id="", mode=MODE_OFF)

    store = ConsentStore(config_dir(env, host_platform=host_platform) / CONFIG_FILENAME)
    consent = store.load()
    if consent is None:
        consent = Consent(install_id=str(uuid.uuid4()))
        store.save(consent)
    return consent


def save_machine(
    env: Mapping[str, str],
    consent: Consent,
    *,
    host_platform: str,
    facts: MachineFacts,
    plugin_version: str,
) -> Consent:
    """Persist a freshly probed (or reused) machine snapshot alongside the switches."""
    updated = replace(consent, machine=facts, machine_plugin_version=plugin_version)
    if consent.mode != MODE_OFF:
        store = ConsentStore(config_dir(env, host_platform=host_platform) / CONFIG_FILENAME)
        store.save(updated)
    return updated
