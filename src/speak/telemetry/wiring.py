"""What every plugin's telemetry composition shares: the HTTP transport, the harness and
locale detection, and the install row brought up to date - the same role `runtime`
plays for logging and the speech server.
"""

import logging
import re
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx2

from speak.telemetry import consent, machine, store
from speak.telemetry.consent import Consent
from speak.telemetry.identity import TelemetryIdentity
from speak.telemetry.schema import TelemetrySchema
from speak.telemetry.ship import TransportResponse

logger = logging.getLogger(__name__)


async def http_transport(url: str, body: bytes) -> TransportResponse:
    """POST one JSON body. Callers treat any transport failure as retryable, so a
    request error is raised as `OSError` rather than left as an httpx-specific type
    they do not know to catch."""
    try:
        async with httpx2.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, content=body, headers={"content-type": "application/json"}
            )
    except httpx2.HTTPError as error:
        raise OSError(str(error)) from error
    return TransportResponse(status=response.status_code, body=response.content)


# The contract's `harness` enum.
HARNESSES = frozenset({"claude-code", "antigravity", "gemini-cli", "codex", "other", "unknown"})


def detect_harness(env: Mapping[str, str], identity: TelemetryIdentity) -> tuple[str, str | None]:
    """(harness, harness_version) from environment signals - spec 2's tiers 2 and 3.

    The MCP `client_info` a session negotiates is the better source (tier 1), but it
    is only available once a request is in flight, not at session start where the
    `sessions` row is first written. A plugin corrects the row with the MCP
    `client_info` on its first tool call, via `normalize_harness`; a one-shot process
    that is no MCP session names its host in `<PREFIX>_HARNESS`, read first and taken
    only when it is a value of the enum.
    Claude Code sets `CLAUDE_PLUGIN_ROOT` for the servers it launches. Anything else
    is reported honestly as "unknown" rather than guessed.
    """
    declared = (env.get(identity.harness_env) or "").strip().lower()
    if declared in HARNESSES:
        return declared, None
    if env.get("CLAUDE_PLUGIN_ROOT"):
        return "claude-code", None
    return "unknown", None


# Claude Code declares itself as "claude-code"; a name that merely contains
# "claude" (Claude Desktop, a third-party "claude-*" client) is another host.
_HARNESS_NAME_HINTS = (
    ("claude-code", "claude-code"),
    ("claude code", "claude-code"),
    ("antigravity", "antigravity"),  # declares itself as "antigravity-client"
    ("gemini", "gemini-cli"),
    ("codex", "codex"),
)


def normalize_harness(name: str) -> str:
    """MCP `client_info.name` into the `harness` enum - spec 2 tier 1.

    A name that does not match a known harness is `"other"`, not `"unknown"`:
    the client declared itself, just not as one of the hosts this schema names.
    `"unknown"` stays reserved for "nothing was declared at all" (spec 2: a
    dashboard rendering `unknown` as a guess would manufacture the very
    conclusion this column exists to test).
    """
    lowered = name.lower()
    for hint, harness in _HARNESS_NAME_HINTS:
        if hint in lowered:
            return harness
    return "other"


# The shapes and lengths the contract accepts for these install fields and session
# columns (`tests/contract/v1/schema/ingest-request.schema.json`). The install and
# its sessions travel with every chunk, so one value outside them would have every
# chunk refused; a value that does not fit is dropped or cut here instead.
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
LANGUAGE_MAX_LENGTH = 35
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){1,2}$")
TIMEZONE_MAX_LENGTH = 64
MACHINE_NAME_MAX_LENGTH = 128  # cpu_model, accel_name

# Zone names that `/etc/localtime` can point at with no area, and their IANA name
# under `Etc/`, which is the only form the contract's pattern accepts.
_AREALESS_ZONES = frozenset({"UTC", "UCT", "GMT", "Universal", "Zulu", "Greenwich"})


def language(env: Mapping[str, str]) -> str | None:
    """The BCP-47 tag from `LANG`, or `None` for anything that is not one.

    `C` and `POSIX` are the absence of a locale, not a language, and a value with a
    modifier (`en_US@euro`) or any other shape is not a tag the contract accepts.
    """
    lang = (env.get("LANG") or "").split(".")[0].replace("_", "-")
    if len(lang) > LANGUAGE_MAX_LENGTH or not LANGUAGE_PATTERN.fullmatch(lang):
        return None
    return lang


def timezone_name(raw: str | None) -> str | None:
    """`raw` as an IANA name the contract accepts, or `None`."""
    if raw is None:
        return None
    name = f"Etc/{raw}" if raw in _AREALESS_ZONES else raw
    if len(name) > TIMEZONE_MAX_LENGTH or not TIMEZONE_PATTERN.fullmatch(name):
        return None
    return name


def local_timezone() -> str | None:
    """The IANA zone name, read from where `/etc/localtime` points.

    A symlink read, not a subprocess - cheap enough to run unconditionally. Windows
    has no equivalent this module can read cheaply, so it reports nothing there.
    """
    try:
        target = str(Path("/etc/localtime").readlink())
    except OSError:
        return None
    marker = "zoneinfo/"
    index = target.find(marker)
    return timezone_name(target[index + len(marker) :]) if index != -1 else None


def utc_offset_min() -> int | None:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else None


@dataclass(frozen=True, slots=True)
class Install:
    """The local store, opened, with this install's row brought up to date."""

    conn: sqlite3.Connection
    consent: Consent


def start_install(
    env: Mapping[str, str],
    identity: TelemetryIdentity,
    schema: TelemetrySchema,
    *,
    db_path: Path,
    plugin_version: str,
    host_platform: str | None = None,
) -> Install | None:
    """Consent, the machine probe and the install row, or `None` when telemetry is
    off or the store cannot be opened.

    Reads nothing from `os.environ`: the mapping comes from the plugin.
    """
    platform_name = host_platform if host_platform is not None else sys.platform
    consented = consent.load_consent(env, identity, host_platform=platform_name)
    if not consented.collects:
        return None
    facts = machine.ensure_current(
        consented.machine,
        cached_plugin_version=consented.machine_plugin_version,
        plugin_version=plugin_version,
        host_platform=platform_name,
    )
    consented = consent.save_machine(
        env,
        identity,
        consented,
        host_platform=platform_name,
        facts=facts,
        plugin_version=plugin_version,
    )
    try:
        conn = store.connect(db_path, schema)
    except OSError as error:
        logger.debug("telemetry: could not open the local store: %s", error)
        return None
    now = store.now_iso()
    existing = store.install_row(conn, consented.install_id)
    store.upsert_install(
        conn,
        {
            "install_id": consented.install_id,
            "first_seen_at": existing["first_seen_at"] if existing else now,
            "last_seen_at": now,
            "plugin_version": plugin_version,
            "disclosure_ver": consented.disclosure_ver,
            "language": language(env),
            "timezone": local_timezone(),
            "utc_offset_min": utc_offset_min(),
            **facts.to_mapping(),
            "cpu_model": facts.cpu_model[:MACHINE_NAME_MAX_LENGTH],
            "accel_name": facts.accel_name[:MACHINE_NAME_MAX_LENGTH],
        },
    )
    return Install(conn=conn, consent=consented)
