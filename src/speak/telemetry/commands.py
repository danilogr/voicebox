"""This plugin's telemetry console scripts: `speak-telemetry` and `speak-stats`.

The only telemetry modules that read `os.environ` and `sys.argv`; everything they
call takes them as arguments.
"""

import os
import sys

from speak.telemetry import cli, report
from speak.telemetry.contract import IDENTITY, SCHEMA, db_path
from speak.telemetry.stats import render


def telemetry_main() -> None:
    raise SystemExit(
        cli.run(
            IDENTITY,
            sys.argv[1:],
            dict(os.environ),
            host_platform=sys.platform,
            allow_record=True,
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
            db_path=db_path(env, host_platform=sys.platform),
            render=render,
            out=sys.stdout,
        )
    )
