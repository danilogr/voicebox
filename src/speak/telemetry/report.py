"""The stats scaffolding every plugin's `<plugin>-stats` command uses: find the local
store, render it with the plugin's own function, and say which install and mode it
belongs to. Renders in every mode that collects, so a user who never uploads still
gets their own dashboard. Writes to the stream its caller passes."""

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from speak.telemetry import consent, store
from speak.telemetry.identity import TelemetryIdentity
from speak.telemetry.schema import TelemetrySchema


def run(
    identity: TelemetryIdentity,
    env: Mapping[str, str],
    *,
    host_platform: str,
    schema: TelemetrySchema,
    db_path: Path,
    render: Callable[[sqlite3.Connection], str],
    out: TextIO,
) -> int:
    """Write the plugin's rendering of its local store, then its install id and mode."""
    if not db_path.exists():
        out.write("No telemetry recorded yet.\n")
        return 0
    conn = store.connect(db_path, schema)
    try:
        out.write(render(conn) + "\n")
        saved = consent.ConsentStore(
            consent.consent_path(env, identity, host_platform=host_platform),
            disclosure_version=identity.disclosure_version,
        ).load()
        if saved is not None:
            out.write(f"\ninstall id: {saved.install_id}\n")
            out.write(f"mode: {saved.mode}" + (" (recording audio)" if saved.record else "") + "\n")
    finally:
        conn.close()
    return 0
