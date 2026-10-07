"""voicebox's telemetry for one MCP session: the install brought up to date by
`wiring.start_install`, then this session's own row. Reads nothing from `os.environ`:
`__main__` passes the environment in."""

import importlib.metadata
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from speak_core.telemetry import store, wiring

from voicebox.config import Settings
from voicebox.telemetry.contract import IDENTITY, SCHEMA, db_path
from voicebox.telemetry.session import TelemetrySession

logger = logging.getLogger(__name__)

# The contract's `semver` pattern and length: a value outside it would cost every chunk
# its session travels in, so it is omitted (nemo) or replaced (core) instead.
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
SEMVER_MAX_LENGTH = 32
UNKNOWN_VERSION = "0.0.0"
# The contract's release version (the ping's `plugin_version`): no pre-release or build
# suffix. A build that is not one never sends anything - see `versions_shippable`.
STRICT_SEMVER = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")

# `nemo-speech --version` answers at once; this only bounds a binary that hangs, so a
# session never waits on it for longer than a blink.
NEMO_VERSION_TIMEOUT_S = 2.0

Run = Callable[..., "subprocess.CompletedProcess[str]"]

# SQLite's answers for a file that is not, or no longer, a database: the store is
# damaged, not busy or out of reach.
_CORRUPT = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})
# What a damaged store is renamed to, beside it, with SQLite's own sidecar files.
CORRUPT_SUFFIX = ".corrupt"
_SIDECARS = ("", "-journal", "-wal", "-shm")


def is_corrupt(error: sqlite3.Error) -> bool:
    """Whether `error` says the store's file is damaged."""
    code = getattr(error, "sqlite_errorcode", None)
    # The extended code carries the primary one in its low byte.
    return isinstance(code, int) and code & 0xFF in _CORRUPT


def set_aside(path: Path) -> None:
    """Rename a damaged store and its sidecars to `<name>.corrupt*`, replacing an earlier
    one, so the next connection starts an empty store.

    Raises:
        OSError: a file could not be renamed.
    """
    for sidecar in _SIDECARS:
        found = path.with_name(path.name + sidecar)
        if found.exists():
            found.replace(path.with_name(path.name + CORRUPT_SUFFIX + sidecar))


def semver_or_none(value: str | None) -> str | None:
    """`value` stripped, if it is the contract's semver; `fullmatch`, because Python's `$`
    also matches before a trailing newline."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > SEMVER_MAX_LENGTH or not SEMVER.fullmatch(value):
        return None
    return value


def core_version() -> str:
    """The speak-core version this plugin pinned. Required by the contract, so a value
    that cannot be read is `0.0.0` rather than missing."""
    try:
        found = importlib.metadata.version("speak-core")
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN_VERSION
    return semver_or_none(found) or UNKNOWN_VERSION


def versions_shippable(plugin_version: str) -> bool:
    """Whether this build's own versions are release versions the server accepts. A
    development build (`1.1.0.dev0`, a local core) collects locally but sends nothing,
    rather than having every request refused."""
    return all(
        STRICT_SEMVER.fullmatch(version.strip()) for version in (plugin_version, core_version())
    )


def read_nemo_version(binary: str, *, run: Run = subprocess.run) -> str | None:
    """The second field of `nemo-speech --version`'s first line, the way
    `install-nemo.sh` reads it, or `None` when it is missing, fails, or is not semver."""
    try:
        result = run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=NEMO_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first = (result.stdout.splitlines() or [""])[0].split()
    return semver_or_none(first[1] if len(first) > 1 else None)


def build(
    env: Mapping[str, str],
    *,
    settings: Settings,
    plugin_version: str,
    host_platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    nemo_version: Callable[[str], str | None] = read_nemo_version,
) -> TelemetrySession | None:
    """The telemetry session for one process, or `None` when telemetry does not collect."""
    platform_name = host_platform if host_platform is not None else sys.platform
    path = db_path(settings.state_dir)

    def start() -> wiring.Install | None:
        return wiring.start_install(
            env,
            IDENTITY,
            SCHEMA,
            db_path=path,
            plugin_version=plugin_version,
            host_platform=platform_name,
        )

    try:
        try:
            install = start()
        except sqlite3.Error as error:
            if not is_corrupt(error):
                raise
            # Damaged for good: left in place, it would stop collection in every session
            # from now on. Its unshipped rows are lost with it.
            set_aside(path)
            logger.warning(
                "telemetry: the local store was damaged (%s); moved to %s%s, starting anew",
                error,
                path.name,
                CORRUPT_SUFFIX,
            )
            install = start()
    except (OSError, sqlite3.Error) as error:
        # A locked or unreachable store must not stop the server: telemetry is optional.
        logger.warning("telemetry: the local store is unusable; collecting nothing: %s", error)
        return None
    if install is None:
        return None
    harness, harness_version = wiring.detect_harness(env, IDENTITY)
    session_id = str(uuid.uuid4())
    try:
        store.insert_session(
            install.conn,
            SCHEMA,
            {
                "session_id": session_id,
                "install_id": install.consent.install_id,
                "started_at": store.now_iso(),
                "plugin_version": plugin_version,
                "core_version": core_version(),
                "nemo_version": nemo_version(settings.server.binary),
                "harness": harness,
                "harness_version": harness_version,
                "ffmpeg_present": which("ffmpeg") is not None and which("ffprobe") is not None,
            },
        )
    except sqlite3.Error as error:
        logger.warning("telemetry: could not record the session; collecting nothing: %s", error)
        install.conn.close()
        return None
    return TelemetrySession(
        conn=install.conn,
        install_id=install.consent.install_id,
        session_id=session_id,
        db_path=path,
    )
