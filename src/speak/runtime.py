"""What every plugin's entry points build the same way: logging, the speech server's
health probe, and the supervisor for the one shared speech server."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable

import httpx2

from speak.config import ServerSettings
from speak.supervisor import NemoSupervisor, Probe, Spawn, process_is_alive, spawn_server

HEALTH_TIMEOUT = 30.0
HEALTH_INTERVAL = 0.5

# The package these modules live in. Its loggers need the handler as much as the
# plugin's do, and the two are different packages.
CORE_LOGGER = __name__.partition(".")[0]


def configure_logging(plugin: str) -> None:
    """Log to stderr only, for the plugin's loggers and this package's. stdout carries
    the protocol and nothing else.

    Both, because a logger with no handler of its own falls through to Python's
    last-resort handler, which drops everything below WARNING.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(f"[{plugin}] %(message)s"))
    for name in dict.fromkeys((plugin, CORE_LOGGER)):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def health_probe(base_url: str, timeout: float = HEALTH_TIMEOUT) -> Probe:
    """Poll until the speech server answers, so the start lock covers the real warmup."""

    async def probe() -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx2.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(f"{base_url.rstrip('/')}/health")
                except httpx2.HTTPError:
                    pass
                else:
                    if response.is_success:
                        return True
                await asyncio.sleep(HEALTH_INTERVAL)
        return False

    return probe


def supervisor_for(
    server: ServerSettings,
    *,
    spawn: Spawn = spawn_server,
    probe: Probe | None = None,
    pid: int | None = None,
    is_alive: Callable[[int], bool] = process_is_alive,
    kill: Callable[[int, int], None] = os.kill,
) -> NemoSupervisor:
    """The supervisor for the one shared speech server, built from the shared settings.

    Only `ServerSettings` goes in, so the server a plugin would start is the same
    whichever plugin starts it: it requests both models on the shared port with the
    shared seed, and keeps its lock, pid record and client registry in the shared
    state directory.
    """
    return NemoSupervisor(
        state_dir=server.shared_dir,
        spawn=spawn,
        probe=probe if probe is not None else health_probe(server.url),
        port=server.port,
        binary=server.binary,
        model=server.tts_model,
        asr_model=server.asr_model,
        seed=server.seed,
        pid=pid,
        is_alive=is_alive,
        kill=kill,
    )
