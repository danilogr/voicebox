"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import signal
import sys
from typing import Protocol

import httpx2
from mcp.server.mcpserver import MCPServer

from speak.config import Settings
from speak.engines import build_engine
from speak.engines.asr import NemoTranscriber
from speak.miclock import MicLock
from speak.queue import SpeechQueue, UtteranceOutcome
from speak.runtime import (
    build_cue,
    build_recorder,
    capture_limits,
    configure_logging,
    health_probe,
    transcription_timeout,
)
from speak.server import VERSION as PLUGIN_VERSION
from speak.server import Ears, create_server
from speak.supervisor import NemoSupervisor, process_is_alive
from speak.telemetry import consent as telemetry_consent
from speak.telemetry import ship as telemetry_ship
from speak.telemetry import store as telemetry_store
from speak.telemetry import wiring as telemetry_wiring
from speak.transcribe.service import build_scribe

logger = logging.getLogger(__name__)

_background: set[asyncio.Task[object]] = set()


# Where telemetry goes unless `SPEAK_TELEMETRY_ENDPOINT` overrides it.
DEFAULT_TELEMETRY_ENDPOINT = "https://speak-telemetry.actionableintelligence.app/v1/ingest"


def build_ears(settings: Settings) -> Ears:
    """Everything the listen tool needs.

    Unconditional: `/speak:converse` and `/talk` are part of the plugin, so they
    cannot depend on a switch the user has to discover. When transcription is not
    actually available the `listen` tool says so and names the remedy.
    """
    return Ears(
        recorder=build_recorder(settings),
        transcriber=NemoTranscriber(
            base_url=settings.nemo_url,
            api_key=settings.nemo_api_key,
            model=settings.nemo_asr_model,
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
    if not settings.nemo_autostart:
        return None
    return NemoSupervisor(
        state_dir=settings.state_dir,
        port=settings.nemo_port,
        binary=settings.nemo_binary,
        model=settings.nemo_model,
        asr_model=settings.nemo_asr_model,
        seed=settings.seed,
        probe=health_probe(settings.nemo_url),
    )


async def _http_transport(url: str, body: bytes) -> telemetry_ship.TransportResponse:
    """POST one chunk. `ship.py` treats any transport failure as retryable, so a
    request error is raised as `OSError` rather than left as an httpx-specific type
    the caller does not know to catch."""
    try:
        async with httpx2.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, content=body, headers={"content-type": "application/json"}
            )
    except httpx2.HTTPError as error:
        raise OSError(str(error)) from error
    return telemetry_ship.TransportResponse(status=response.status_code, body=response.content)


def _debug_requested(env: dict[str, str]) -> bool:
    return (env.get("SPEAK_TELEMETRY_DEBUG") or "").strip() not in ("", "0")


async def _ship_backlog() -> None:
    """Ship once, at session start, off the hot path - failures leave rows
    unshipped for the next session to retry (spec 7, 11)."""
    env = dict(os.environ)
    debug = _debug_requested(env)
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
        url = env.get("SPEAK_TELEMETRY_ENDPOINT") or DEFAULT_TELEMETRY_ENDPOINT
        shipped = await telemetry_ship.ship(
            conn,
            install_id=consented.install_id,
            url=url,
            transport=_http_transport,
            debug=debug,
        )
        logger.debug("telemetry: shipped %d utterance row(s)", shipped)
    except Exception:
        logger.debug("telemetry: shipping failed", exc_info=True)
    finally:
        conn.close()


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

    engine = build_engine(settings, on_unavailable=start_speech_server)

    telemetry = telemetry_wiring.build(os.environ, settings=settings, plugin_version=PLUGIN_VERSION)

    def on_finish(outcome: UtteranceOutcome) -> None:
        if telemetry is not None:
            telemetry.utterance_finished(outcome)

    queue = SpeechQueue(prepare=engine.prepare, on_finish=on_finish)

    def schedule_ship() -> None:
        """Deferred to the server's lifespan: `build()` runs before `server.run()`
        starts an event loop, so `asyncio.create_task` cannot be called from here."""
        if telemetry is None:
            return
        task = asyncio.create_task(_ship_backlog())
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
        on_session_start=schedule_ship,
    )


class StdioServer(Protocol):
    async def run_stdio_async(self) -> None: ...


# What a host sends to end an MCP server it launched, and what a closing terminal
# sends. Left at their defaults either one ends the process on the spot.
STOP_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


async def serve_stdio(server: StdioServer) -> None:
    """Serve on stdio until the client closes it or the process is told to stop.

    A stop signal's default action kills the process without unwinding it, which
    skips the lifespan's shutdown: the speaker keeps talking, the shared speech
    server is never released, and the session row never gets its `ended_at`. Each
    one is turned into a cancellation of this task instead, which unwinds through
    the lifespan the way Ctrl-C does. Only the first signal counts, so a second one
    cannot cut that shutdown short.

    The shutdown runs at once, but the process exits only when stdin closes: the MCP
    transport reads it on a worker thread that no signal interrupts, and the
    interpreter waits for that thread before it exits.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    received: list[int] = []

    def stop(signum: int) -> None:
        if not received and task is not None:
            received.append(signum)
            task.cancel()

    for signum in STOP_SIGNALS:
        try:
            loop.add_signal_handler(signum, stop, signum)
        except (NotImplementedError, RuntimeError):  # no loop signal support here
            logger.debug("cannot handle signal %s; it will end the process", signum)
    try:
        await server.run_stdio_async()
    except asyncio.CancelledError:
        if not received:
            raise
        logger.debug("stopped by signal %d", received[0])
    finally:
        for signum in STOP_SIGNALS:
            loop.remove_signal_handler(signum)


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    try:
        server = build(settings)
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error
    asyncio.run(serve_stdio(server))


if __name__ == "__main__":
    main()
