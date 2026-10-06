"""`speak-core-setup`: install NeMo-Speech.cpp and the models every plugin uses.

The installer is a shell script shipped inside the package, so a plugin's `/setup`
runs it from the plugin's own environment wherever the package was installed from.
It reads `NEMO_BIN` and `NEMO_INSTALLER` itself, from the environment it inherits.
"""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

SCRIPT = Path(__file__).with_name("install-nemo.sh")

Runner = Callable[[Sequence[str]], int]


def _run(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def run(*, runner: Runner = _run) -> int:
    """Run the installer with bash and return its exit status."""
    return runner(["bash", str(SCRIPT)])


def main() -> None:
    raise SystemExit(run())
