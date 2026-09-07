"""Lifecycle for the one speech server shared by every session.

The server holds about a gigabyte of resident memory, so sessions share a single
instance rather than each starting their own. Sessions are separate processes, so
they coordinate through files under the state directory.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_LOCK_SECONDS = 60.0

Spawn = Callable[[str, Sequence[str]], Awaitable[int]]
Probe = Callable[[], Awaitable[bool]]


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


async def spawn_server(binary: str, args: Sequence[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid


class NemoSupervisor:
    """Starts the speech server once, shares it, and stops it when nobody is left."""

    def __init__(
        self,
        *,
        state_dir: Path,
        spawn: Spawn = spawn_server,
        probe: Probe,
        port: int = 8082,
        binary: str = "nemo-speech",
        model: str = "magpie",
        seed: int | None = None,
        pid: int | None = None,
        stale_lock_seconds: float = STALE_LOCK_SECONDS,
        is_alive: Callable[[int], bool] = process_is_alive,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._clients_dir = self._state_dir / "clients"
        self._lock_dir = self._state_dir / "starting"
        self._server_file = self._state_dir / "server.json"
        self._spawn = spawn
        self._probe = probe
        self._port = port
        self._binary = binary
        self._model = model
        self._seed = seed
        self._pid = pid if pid is not None else os.getpid()
        self._stale_lock_seconds = stale_lock_seconds
        self._is_alive = is_alive
        self._kill = kill

    def _server_args(self) -> list[str]:
        args = [
            "serve",
            "--tts-model",
            self._model,
            "--port",
            str(self._port),
            "--device",
            "cpu",
            "--no-ui",
        ]
        if self._seed is not None:
            args += ["--tts.seed", str(self._seed)]
        return args

    def _take_lock(self) -> bool:
        """Claim the right to start the server. `mkdir` is atomic, so exactly one wins."""
        try:
            self._lock_dir.mkdir(parents=True)
        except FileExistsError:
            age = time.time() - self._lock_dir.stat().st_mtime
            if age < self._stale_lock_seconds:
                return False
            logger.info("taking over a start lock left by a session that never finished")
            shutil.rmtree(self._lock_dir, ignore_errors=True)
            return self._take_lock()
        return True

    async def register(self) -> None:
        self._clients_dir.mkdir(parents=True, exist_ok=True)
        (self._clients_dir / str(self._pid)).touch()

    async def ensure_running(self) -> bool:
        """Start the server unless another session is already starting it."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if not self._take_lock():
            return False
        try:
            server_pid = await self._spawn(self._binary, self._server_args())
            self._server_file.write_text(
                json.dumps({"pid": server_pid, "port": self._port, "started_at": time.time()})
            )
            await self._probe()
            return True
        finally:
            shutil.rmtree(self._lock_dir, ignore_errors=True)

    async def release(self) -> bool:
        """Leave, and stop the server if this was the last session using it."""
        (self._clients_dir / str(self._pid)).unlink(missing_ok=True)

        if not self._prune_dead_clients():
            return False

        try:
            recorded = json.loads(self._server_file.read_text())
        except (OSError, ValueError):
            return False

        logger.info("last session out, stopping speech server %s", recorded["pid"])
        try:
            self._kill(recorded["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as error:
            logger.info("speech server was already gone: %s", error)
        self._server_file.unlink(missing_ok=True)
        return True

    def _prune_dead_clients(self) -> bool:
        """Drop client files for sessions that are gone. True when none are left."""
        try:
            entries = list(self._clients_dir.iterdir())
        except OSError:
            return True

        for entry in entries:
            if not entry.name.isdigit() or not self._is_alive(int(entry.name)):
                entry.unlink(missing_ok=True)
        return not any(self._clients_dir.iterdir())
