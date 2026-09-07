"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import sys

import httpx2
from mcp.server.mcpserver import MCPServer

from speak.config import Settings
from speak.engines import build_engine
from speak.queue import SpeechQueue
from speak.server import create_server
from speak.supervisor import NemoSupervisor, Probe

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT = 30.0
HEALTH_INTERVAL = 0.5

_background: set[asyncio.Task[object]] = set()


def configure_logging() -> None:
    """Log to stderr only. stdout carries the protocol and nothing else."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[speak] %(message)s"))
    root = logging.getLogger("speak")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


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


def build(settings: Settings) -> MCPServer:
    """Wire the engine, queue and supervisor together."""
    supervisor: NemoSupervisor | None = None
    if settings.engine == "nemo" and settings.nemo_autostart:
        supervisor = NemoSupervisor(
            state_dir=settings.state_dir,
            port=settings.nemo_port,
            binary=settings.nemo_binary,
            model=settings.nemo_model,
            seed=settings.seed,
            probe=health_probe(settings.nemo_url),
        )

    def start_speech_server() -> None:
        """Kick off a cold start without waiting: this sentence goes out through `say`."""
        if supervisor is None:
            return
        task = asyncio.create_task(supervisor.ensure_running())
        _background.add(task)
        task.add_done_callback(_background.discard)

    engine = build_engine(settings, on_unavailable=start_speech_server)
    queue = SpeechQueue(prepare=engine.prepare)
    return create_server(settings=settings, engine=engine, queue=queue, supervisor=supervisor)


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    try:
        server = build(settings)
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
