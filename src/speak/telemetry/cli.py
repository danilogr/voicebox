"""The consent command every plugin registers as its own `<plugin>-telemetry`.

`on`, `local` and `off` persist the mode; `--id` prints the install id; with no
argument it reports the saved mode. `record on|off` exists only for a plugin that
keeps recordings. Output goes to the streams the caller passes.

Saving a mode before the plugin has ever run creates the consent file, so that
install's first ping reports an upgrade rather than an install.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TextIO

from speak.telemetry import consent
from speak.telemetry.identity import TelemetryIdentity


def _usage(identity: TelemetryIdentity, *, allow_record: bool) -> str:
    commands = "on|local|off|--id" + ("|record on|record off" if allow_record else "")
    return f"usage: {identity.plugin}-telemetry [{commands}]\n"


def run(
    identity: TelemetryIdentity,
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    host_platform: str,
    allow_record: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    path = consent.consent_path(env, identity, host_platform=host_platform)
    store = consent.ConsentStore(path, disclosure_version=identity.disclosure_version)
    current = store.load()
    command = list(argv)

    if not command:
        mode = current.mode if current is not None else f"{consent.DEFAULT_MODE} (default)"
        line = f"{identity.plugin} telemetry: {mode}"
        if current is not None and current.record:
            line += ", keeping recordings"
        if consent.forced_off(env, identity):
            line += f" (overridden to off here by {identity.mode_env} or DO_NOT_TRACK)"
        out.write(line + "\n")
        return 0

    if command == ["--id"]:
        if current is None or not current.install_id:
            err.write(f"no install id yet: {identity.plugin} creates one the first time it runs\n")
            return 1
        out.write(current.install_id + "\n")
        return 0

    base = current or consent.Consent(
        install_id=str(uuid.uuid4()), disclosure_ver=identity.disclosure_version
    )
    if len(command) == 1 and command[0] in consent.MODES:
        updated = replace(base, mode=command[0])
        message = f"{identity.plugin} telemetry is now {command[0]}."
        if command[0] != consent.MODE_ON:
            message += " The anonymous install ping still goes; set DO_NOT_TRACK=1 to stop it too."
    elif (
        allow_record
        and len(command) == 2
        and command[0] == "record"
        and command[1] in ("on", "off")
    ):
        updated = replace(base, record=command[1] == "on")
        message = f"{identity.plugin} recordings are now {command[1]}."
    else:
        err.write(_usage(identity, allow_record=allow_record))
        return 2

    if not store.save(updated):
        err.write(f"could not save {path}\n")
        return 1
    out.write(message + "\n")
    return 0
