"""voicebox's telemetry console scripts: `voicebox-telemetry` and `voicebox-stats`.

The only telemetry modules that read `os.environ` and `sys.argv`; everything they call
takes them as arguments.
"""

import os
import sys
from collections.abc import Mapping
from typing import TextIO

from speak_core.telemetry import cli, consent, report
from speak_core.telemetry import notice as core_notice

from voicebox.config import Settings
from voicebox.telemetry.contract import IDENTITY, SCHEMA, db_path
from voicebox.telemetry.notice import TEXT
from voicebox.telemetry.stats import render


def show_notice(env: Mapping[str, str], *, host_platform: str, out: TextIO) -> int:
    """Print the first-run notice and record it as shown - what `/voicebox:setup` runs."""
    if consent.do_not_track(env):
        out.write("DO_NOT_TRACK is set: voicebox collects nothing and sends nothing.\n")
        return 0
    out.write(TEXT + "\n")
    core_notice.mark_shown(env, IDENTITY, host_platform=host_platform)
    return 0


def telemetry_main() -> None:
    env = dict(os.environ)
    argv = sys.argv[1:]
    if argv == ["notice"]:
        raise SystemExit(show_notice(env, host_platform=sys.platform, out=sys.stdout))
    raise SystemExit(
        cli.run(
            IDENTITY,
            argv,
            env,
            host_platform=sys.platform,
            allow_record=False,
            out=sys.stdout,
            err=sys.stderr,
        )
    )


def stats_main() -> None:
    env = dict(os.environ)
    raise SystemExit(
        report.run(
            IDENTITY,
            env,
            host_platform=sys.platform,
            schema=SCHEMA,
            db_path=db_path(Settings.from_env(env).state_dir),
            render=render,
            out=sys.stdout,
        )
    )
