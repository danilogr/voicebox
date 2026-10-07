"""voicebox's telemetry console scripts: `voicebox-telemetry` and `voicebox-stats`.

The only telemetry modules that read `os.environ` and `sys.argv`; everything they call
takes them as arguments.
"""

import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from speak_core.telemetry import cli, consent, report
from speak_core.telemetry import notice as core_notice

from voicebox.config import Settings
from voicebox.telemetry.compose import CORRUPT_SUFFIX, is_corrupt
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
    path = db_path(Settings.from_env(env).state_dir)
    try:
        code = report.run(
            IDENTITY,
            env,
            host_platform=sys.platform,
            schema=SCHEMA,
            db_path=path,
            render=render,
            out=sys.stdout,
        )
    except sqlite3.Error as error:
        sys.stderr.write(unreadable(path, error) + "\n")
        code = 1
    raise SystemExit(code)


def unreadable(path: Path, error: sqlite3.Error) -> str:
    """What `/voicebox:stats` says about a store it cannot read."""
    if is_corrupt(error):
        return (
            f"The telemetry store {path} is damaged and cannot be read ({error}). The next "
            f"voicebox session moves it aside to {path.name}{CORRUPT_SUFFIX} and starts a "
            "new one; the jobs recorded in it are not shown."
        )
    return f"The telemetry store {path} cannot be read right now ({error}); try again."
