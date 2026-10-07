"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import signal
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from speak_core.engines import build_engine
from speak_core.queue import SpeechQueue
from speak_core.runtime import configure_logging, supervisor_for
from speak_core.speaker import SpeakerLock
from speak_core.supervisor import NemoSupervisor

from voicebox.config import PLUGIN, Settings
from voicebox.server import create_server
from voicebox.transcribe.service import build_scribe

logger = logging.getLogger(__name__)

_background: set[asyncio.Task[object]] = set()


def build_supervisor(settings: Settings) -> NemoSupervisor | None:
    """The shared speech server this session may start, or None when the user runs it."""
    if not settings.server.autostart:
        return None
    return supervisor_for(settings.server)


def build(settings: Settings) -> MCPServer:
    """Wire the engine, queue, supervisor and transcription together."""
    supervisor = build_supervisor(settings)

    def start_speech_server() -> None:
        """Kick off a cold start without waiting: this sentence goes out through `say`."""
        if supervisor is None:
            return
        task = asyncio.create_task(supervisor.ensure_running())
        _background.add(task)
        task.add_done_callback(_background.discard)

    engine = build_engine(settings.speech, settings.server, on_unavailable=start_speech_server)
    queue = SpeechQueue(
        prepare=engine.prepare,
        speaker=SpeakerLock(shared_dir=settings.server.shared_dir),
    )
    return create_server(
        settings=settings,
        engine=engine,
        queue=queue,
        supervisor=supervisor,
        scribe=build_scribe(settings),
    )


class StdioServer(Protocol):
    async def run_stdio_async(self) -> None: ...


# What a host sends to end an MCP server it launched, and what a closing terminal
# sends. Left at their defaults either one ends the process on the spot.
STOP_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


async def serve_stdio(server: StdioServer) -> int | None:
    """Serve on stdio until the client closes it or the process is told to stop.

    A stop signal's default action kills the process without unwinding it, which
    skips the lifespan's shutdown: the speaker keeps talking, the shared speech
    server is never released, and the session row never gets its `ended_at`. Each
    one is turned into a cancellation of this task instead, which unwinds through
    the lifespan the way Ctrl-C does. Only the first signal counts, so a second one
    cannot cut that shutdown short.

    Returns the signal that stopped it, or `None` when stdin closed. After a signal
    the MCP transport's stdin reader is still blocked on a worker thread that no
    signal interrupts, so the caller must not leave the exit to the interpreter.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    received: list[int] = []

    def stop(signum: int) -> None:
        if not received and task is not None:
            received.append(signum)
            task.cancel()

    handled: list[int] = []
    for signum in STOP_SIGNALS:
        try:
            loop.add_signal_handler(signum, stop, signum)
        except (NotImplementedError, RuntimeError):  # no loop signal support here
            logger.debug("cannot handle signal %s; it will end the process", signum)
        else:
            handled.append(signum)
    try:
        await server.run_stdio_async()
    except asyncio.CancelledError:
        if not received:
            raise
        logger.debug("stopped by signal %d", received[0])
    finally:
        for signum in handled:
            loop.remove_signal_handler(signum)
    return received[0] if received else None


def run(server: StdioServer) -> None:
    """Serve until stopped, then exit - at once, if a signal did the stopping.

    The interpreter's normal exit joins every non-daemon thread, and the transport's
    stdin reader only returns when stdin closes. A host that signals the process and
    keeps the pipe open would wait for an exit that never comes, so a signalled stop
    ends the process directly once the shutdown has run.
    """
    if asyncio.run(serve_stdio(server)) is not None:
        logging.shutdown()
        os._exit(0)


def main() -> None:
    configure_logging(PLUGIN)
    settings = Settings.from_env(os.environ)
    try:
        server = build(settings)
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error
    run(server)


if __name__ == "__main__":
    main()
