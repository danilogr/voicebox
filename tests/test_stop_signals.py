"""A stop signal unwinds the server through its shutdown instead of killing it.

Run in a child process: the behaviour under test is what a real signal does to a
real process, and a handler that failed to install would otherwise end the suite.
"""

import signal
import subprocess
import sys
from pathlib import Path

import pytest

# A stand-in for the MCP server: it serves until cancelled, and its `finally` does
# what the lifespan's shutdown does - awaits, then writes - so a process killed
# outright leaves the marker unwritten.
CHILD = """
import asyncio, sys
from pathlib import Path
from speak.__main__ import serve_stdio

class Server:
    async def run_stdio_async(self):
        try:
            print("serving", flush=True)
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            Path(sys.argv[1]).write_text("shut down")

asyncio.run(serve_stdio(Server()))
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_runs_the_shutdown_and_exits_cleanly(tmp_path: Path, signum) -> None:
    marker = tmp_path / "marker"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(marker)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "serving"
        child.send_signal(signum)
        assert child.wait(timeout=20) == 0
    finally:
        child.kill()
        child.wait()
    assert marker.read_text() == "shut down"
