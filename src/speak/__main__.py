"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import signal
import sys
from typing import Protocol

from mcp.server.mcpserver import MCPServer

from speak.engines import build_engine
from speak.engines.asr import NemoTranscriber
from speak.listening import build_cue, build_recorder, capture_limits, transcription_timeout
from speak.miclock import MicLock
from speak.queue import SpeechQueue, UtteranceOutcome
from speak.runtime import configure_logging, supervisor_for
from speak.server import VERSION as PLUGIN_VERSION
from speak.server import Ears, create_server
from speak.settings import PLUGIN, Settings
from speak.speaker import SpeakerLock
from speak.supervisor import NemoSupervisor, process_is_alive
from speak.telemetry import consent as telemetry_consent
from speak.telemetry import ping as telemetry_ping
from speak.telemetry import ship as telemetry_ship
from speak.telemetry import store as telemetry_store
from speak.telemetry import wiring as telemetry_wiring
from speak.transcribe.service import build_scribe

logger = logging.getLogger(__name__)

_background: set[asyncio.Task[object]] = set()


def build_ears(settings: Settings) -> Ears:
    """Everything the listen tool needs.

    Unconditional: `/speak:converse` and `/talk` are part of the plugin, so they
    cannot depend on a switch the user has to discover. When transcription is not
    actually available the `listen` tool says so and names the remedy.
    """
    return Ears(
        recorder=build_recorder(settings),
        transcriber=NemoTranscriber(
            base_url=settings.server.url,
            api_key=settings.server.api_key,
            model=settings.server.asr_model,
            timeout=transcription_timeout(settings),
        ),
        mic_lock=MicLock(state_dir=settings.state_dir, is_alive=process_is_alive),
        limits=capture_limits(settings),
        cue=build_cue(settings),
    )


def build_supervisor(settings: Settings) -> NemoSupervisor | None:
    """The shared speech server this session may start, or None when it must not.

    Every session may listen, and listening needs the server whatever the speech
    engine is, so the only thing that stops one being started is the user saying so.
    The transcription model is always requested; a model that will not load is
    handled when the server starts, not by withholding it here.
    """
    if not settings.server.autostart:
        return None
    return supervisor_for(settings.server)


async def _ship_backlog() -> None:
    """Ship once, at session start, off the hot path - failures leave rows
    unshipped for the next session to retry (spec 7, 11)."""
    env = dict(os.environ)
    debug = telemetry_wiring.debug_requested(env)
    consented = telemetry_consent.load_consent(env, host_platform=sys.platform)
    if not consented.collects or not (consented.uploads or debug):
        return
    try:
        conn = telemetry_store.connect(
            telemetry_consent.config_dir(env, host_platform=sys.platform)
            / telemetry_wiring.DB_FILENAME
        )
    except OSError as error:
        logger.debug("telemetry: could not open the local store to ship: %s", error)
        return
    try:
        shipped = await telemetry_ship.ship(
            conn,
            install_id=consented.install_id,
            url=telemetry_wiring.ingest_url(env),
            transport=telemetry_wiring.http_transport,
            debug=debug,
        )
        logger.debug("telemetry: shipped %d utterance row(s)", shipped)
    except Exception:
        logger.debug("telemetry: shipping failed", exc_info=True)
    finally:
        conn.close()


async def _send_ping(ping: telemetry_ping.Ping) -> None:
    """The install ping, in the background. A failure is due again next session."""
    try:
        await telemetry_ping.send(
            ping,
            transport=telemetry_wiring.http_transport,
            debug=telemetry_wiring.debug_requested(os.environ),
        )
    except Exception:
        logger.debug("telemetry: the install ping failed", exc_info=True)


def build(settings: Settings) -> MCPServer:
    """Wire the engine, queue, supervisor and ears together."""
    supervisor = build_supervisor(settings)

    def start_speech_server() -> None:
        """Kick off a cold start without waiting: this sentence goes out through `say`."""
        if supervisor is None:
            return
        task = asyncio.create_task(supervisor.ensure_running())
        _background.add(task)
        task.add_done_callback(_background.discard)

    engine = build_engine(settings.speech, settings.server, on_unavailable=start_speech_server)

    # Decided before `telemetry_wiring.build`, which may create the consent file this
    # reads to tell an install from an upgrade.
    ping = telemetry_ping.due(
        os.environ,
        plugin_version=PLUGIN_VERSION,
        host_platform=sys.platform,
        ingest_url=telemetry_wiring.ingest_url(os.environ),
    )
    telemetry = telemetry_wiring.build(os.environ, settings=settings, plugin_version=PLUGIN_VERSION)

    def on_finish(outcome: UtteranceOutcome) -> None:
        if telemetry is not None:
            telemetry.utterance_finished(outcome)

    queue = SpeechQueue(
        prepare=engine.prepare,
        on_finish=on_finish,
        speaker=SpeakerLock(shared_dir=settings.server.shared_dir),
    )

    def schedule_sends() -> None:
        """Deferred to the server's lifespan: `build()` runs before `server.run()`
        starts an event loop, so `asyncio.create_task` cannot be called from here.

        The ping goes whatever the telemetry mode; the backlog only when telemetry
        collects at all.
        """
        sends = []
        if ping is not None:
            sends.append(_send_ping(ping))
        if telemetry is not None:
            sends.append(_ship_backlog())
        for send in sends:
            task = asyncio.create_task(send)
            _background.add(task)
            task.add_done_callback(_background.discard)

    return create_server(
        settings=settings,
        engine=engine,
        queue=queue,
        supervisor=supervisor,
        ears=build_ears(settings),
        telemetry=telemetry,
        scribe=build_scribe(settings),
        on_session_start=schedule_sends,
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
