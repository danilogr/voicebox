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
# How long to wait for a signalled attempt to exit before retrying anyway. Short:
# the alternative to giving up is hanging the session on a wedged process.
TERMINATE_TIMEOUT = 5.0
TERMINATE_INTERVAL = 0.05

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
        asr_model: str | None = None,
        seed: int | None = None,
        pid: int | None = None,
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
        self._asr_model = asr_model
        self._seed = seed
        self._pid = pid if pid is not None else os.getpid()
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
            "auto",
            "--no-ui",
        ]
        if self._asr_model is not None:
            args += ["--asr-model", self._asr_model]
        if self._seed is not None:
            args += ["--tts.seed", str(self._seed)]
        return args

    def _take_lock(self) -> bool:
        """Claim the right to start the server. `mkdir` is atomic, so exactly one wins.

        One retry, not recursion: `rmtree` ignores errors, so a lock directory that
        cannot be removed - a read-only parent, or one owned by another user - would
        otherwise be retried forever against an mtime that never changes.
        """
        for last_attempt in (False, True):
            try:
                self._lock_dir.mkdir(parents=True)
            except FileExistsError:
                age = time.time() - self._lock_dir.stat().st_mtime
                if age < STALE_LOCK_SECONDS or last_attempt:
                    return False
                logger.info("taking over a start lock left by a session that never finished")
                shutil.rmtree(self._lock_dir, ignore_errors=True)
            else:
                return True
        return False

    async def register(self) -> None:
        self._clients_dir.mkdir(parents=True, exist_ok=True)
        (self._clients_dir / str(self._pid)).touch()

    async def ensure_running(self) -> bool:
        """Start the server unless one is already running or being started.

        Returns True only when this call spawned it. Either way the caller is left
        with a server that has answered, because both early returns still probe: a
        server spawned a moment ago by another session has not bound its port yet,
        and returning before it does turns a several-second cold start into "there is no
        transcription model" at the call site, whose remedies are then all wrong.

        The recorded server is checked here rather than only at the call sites,
        because a caller cannot always tell a dead server from a busy one: the TTS
        fallback fires on any HTTP failure, a 5xx or a timeout from a loaded server
        included. Spawning then would write a second, doomed pid — it cannot bind the
        port — over the record of the server that is still answering, and the probe
        that followed would be answered by the first server and report success. The
        real server would be orphaned and the last session out would stop a pid that
        is already gone, leaving a gigabyte resident.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if self._recorded_server_is_alive():
            logger.info("a speech server is already running; not starting another")
            await self._probe()
            return False
        if not self._take_lock():
            logger.info("another session is starting the speech server; waiting for it")
            await self._probe()
            return False
        try:
            pid, answered = await self._start_once(self._server_args())
            if answered or self._asr_model is None:
                return True
            logger.warning(
                "the speech server did not answer with transcription model %s; "
                "retrying without it so speech still works",
                self._asr_model,
            )
            await self._terminate(pid)
            self._asr_model = None
            await self._start_once(self._server_args())
            return True
        finally:
            shutil.rmtree(self._lock_dir, ignore_errors=True)

    async def _start_once(self, args: list[str]) -> tuple[int, bool]:
        """Spawn the server, record it, and wait for it to answer.

        Returns the pid and whether it answered. The record is rewritten on every
        attempt, so `server.json` always names the process a later `release` should
        stop rather than one that died on startup.
        """
        server_pid = await self._spawn(self._binary, args)
        self._server_file.write_text(
            json.dumps({"pid": server_pid, "port": self._port, "started_at": time.time()})
        )
        return server_pid, bool(await self._probe())

    async def _terminate(self, pid: int) -> None:
        """Stop an attempt that never answered, and wait until it is gone.

        Not merely tidiness: a server that started but is unhealthy still holds the
        port, and the retry binds that same port. SIGTERM is asynchronous, so the
        wait is the load-bearing half - signalling and respawning in the same breath
        races the process being replaced.
        """
        try:
            self._kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as error:
            logger.debug("the failed attempt was already gone: %s", error)
            return
        deadline = asyncio.get_running_loop().time() + TERMINATE_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if not self._is_alive(pid):
                return
            await asyncio.sleep(TERMINATE_INTERVAL)
        logger.warning("attempt %s did not exit; retrying anyway", pid)

    def _recorded_pid(self) -> int | None:
        """The pid in `server.json`, or None when there is no usable one.

        Both readers need the same guards. `release` runs in a `finally` whose caller
        catches a fixed set of exceptions, so anything else raised there escapes, and on
        a one-shot caller an escaped exception prints a traceback where the user's own
        words are read from. The file is on disk, so its shape is not this code's to
        assume: that it parsed is not the same as that it is a record we wrote.
        """
        try:
            recorded = json.loads(self._server_file.read_text())
        except (OSError, ValueError):
            return None
        pid = recorded.get("pid") if isinstance(recorded, dict) else None
        return pid if isinstance(pid, int) else None

    def _recorded_server_is_alive(self) -> bool:
        """True when `server.json` names a process that still exists."""
        pid = self._recorded_pid()
        return pid is not None and self._is_alive(pid)

    async def release(self) -> bool:
        """Leave, and stop the server if this was the last session using it."""
        (self._clients_dir / str(self._pid)).unlink(missing_ok=True)

        if not self._prune_dead_clients():
            return False

        pid = self._recorded_pid()
        if pid is None:
            return False

        logger.info("last session out, stopping speech server %s", pid)
        try:
            self._kill(pid, signal.SIGTERM)
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
