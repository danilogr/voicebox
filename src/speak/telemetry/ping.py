"""The install ping: one anonymous request per install and per upgrade.

Sent in every telemetry mode - `on`, `local` and `off` alike - and never under
`DO_NOT_TRACK`, which skips this module entirely: no request and no file. The body is
the same three fields in every mode, so a ping never says whether its sender opted
out, and it carries no identifier, so two pings cannot be linked to each other or to
an install.

Whether one is due is decided from `ping.json` in the config directory, which holds
only the last version pinged. That file is written only after the server answers
`2xx`, so a ping that fails is simply due again next session; there is no other retry.

Deciding is a file read, done synchronously before anything else touches the config
directory; sending is a background task that never delays speech or a transcript.
Takes an injected transport, so no test here opens a socket.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from speak.telemetry import consent
from speak.telemetry.identity import TelemetryIdentity
from speak.telemetry.ship import CONTRACT_OS, Transport

logger = logging.getLogger(__name__)

MARKER_FILENAME = "ping.json"
PING_PATH = "/v1/ping"


@dataclass(frozen=True, slots=True)
class Ping:
    """One ping that is due: what it says, where it goes, and the marker it settles."""

    payload: dict[str, str]
    url: str
    marker: Path


def ping_url(ingest_url: str) -> str:
    """The ping endpoint: the ingest endpoint's origin, with the ping's own path."""
    parts = urlsplit(ingest_url)
    return f"{parts.scheme}://{parts.netloc}{PING_PATH}"


def _last_pinged(marker: Path) -> str | None:
    """The version the marker records, or `None` when there is no readable one."""
    try:
        version = json.loads(marker.read_text(encoding="utf-8")).get("plugin_version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def due(
    env: Mapping[str, str],
    identity: TelemetryIdentity,
    *,
    plugin_version: str,
    host_platform: str,
    ingest_url: str,
) -> Ping | None:
    """The ping this process owes, or `None`.

    Has to run before the consent file can be created for the first time: with no
    marker, an existing `telemetry.json` is what tells an upgrade from an install
    that predates the ping from a new one.
    """
    if consent.do_not_track(env):
        return None
    if host_platform not in CONTRACT_OS:
        # The contract has no value for any other OS, so the server would refuse it.
        return None
    directory = identity.config_dir(env, host_platform=host_platform)
    marker = directory / MARKER_FILENAME
    last = _last_pinged(marker)
    if last == plugin_version:
        return None
    if last is None and not (directory / consent.CONFIG_FILENAME).exists():
        event = "install"
    else:
        event = "upgrade"
    return Ping(
        payload={"plugin_version": plugin_version, "os": host_platform, "event": event},
        url=ping_url(ingest_url),
        marker=marker,
    )


async def send(ping: Ping, *, transport: Transport, debug: bool = False) -> bool:
    """Send one ping and record it as sent on a `2xx`. True when it was sent.

    `debug` logs the payload instead and writes nothing.
    Never raises: a ping that cannot be sent is due again next session.
    """
    body = json.dumps(ping.payload)
    if debug:
        logger.info("telemetry debug, ping not sent: %s", body)
        return False
    try:
        response = await transport(ping.url, body.encode("utf-8"))
    except OSError as error:
        logger.debug("telemetry: ping failed: %s", error)
        return False
    if not 200 <= response.status < 300:
        logger.debug("telemetry: ping not counted: status %d", response.status)
        return False
    try:
        ping.marker.parent.mkdir(parents=True, exist_ok=True)
        marker = {"plugin_version": ping.payload["plugin_version"]}
        ping.marker.write_text(json.dumps(marker), encoding="utf-8")
    except OSError as error:
        logger.debug("telemetry: could not record the ping at %s: %s", ping.marker, error)
    return True
