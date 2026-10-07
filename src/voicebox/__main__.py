"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Coroutine, Mapping
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from speak_core.engines import build_engine
from speak_core.engines.asr import Liveness, NemoTranscriber
from speak_core.engines.nemo import NemoSynthesizer
from speak_core.queue import SpeechQueue, UtteranceOutcome
from speak_core.runtime import configure_logging, supervisor_for
from speak_core.speaker import SpeakerLock
from speak_core.supervisor import NemoSupervisor
from speak_core.telemetry import consent as telemetry_consent
from speak_core.telemetry import notice as telemetry_notice
from speak_core.telemetry import ping as telemetry_ping
from speak_core.telemetry import ship as telemetry_ship
from speak_core.telemetry import store as telemetry_store
from speak_core.telemetry import wiring as telemetry_wiring

from voicebox.config import PLUGIN, Settings
from voicebox.jobs.desk import build_desk, sweep
from voicebox.narration import NarrationTracker
from voicebox.server import VERSION, create_server
from voicebox.synthesize.service import Speech
from voicebox.telemetry import compose as telemetry_compose
from voicebox.telemetry.contract import IDENTITY, SCHEMA, db_path
from voicebox.telemetry.notice import Notice

logger = logging.getLogger(__name__)

_background: set[asyncio.Task[object]] = set()


def build_supervisor(settings: Settings) -> NemoSupervisor | None:
    """The shared speech server this session may start, or None when the user runs it."""
    if not settings.server.autostart:
        return None
    return supervisor_for(settings.server)


async def _ship_backlog(env: Mapping[str, str], settings: Settings) -> None:
    """Ship once, at session start, off the hot path. A failure leaves rows unshipped
    for the next session to retry."""
    debug = IDENTITY.debug_requested(env)
    consented = telemetry_consent.load_consent(env, IDENTITY, host_platform=sys.platform)
    if not consented.collects or not (consented.uploads or debug):
        return
    try:
        conn = telemetry_store.connect(db_path(settings.state_dir), SCHEMA)
    except (OSError, sqlite3.Error) as error:
        logger.debug("telemetry: could not open the local store to ship: %s", error)
        return
    try:
        shipped = await telemetry_ship.ship(
            conn,
            schema=SCHEMA,
            install_id=consented.install_id,
            url=IDENTITY.ingest_url(env),
            transport=telemetry_wiring.http_transport,
            debug=debug,
        )
        logger.debug("telemetry: shipped %d row(s)", shipped)
    except Exception:
        logger.debug("telemetry: shipping failed", exc_info=True)
    finally:
        conn.close()


async def _send_ping(ping: telemetry_ping.Ping, env: Mapping[str, str]) -> None:
    """The install ping, in the background. A failure is due again next session."""
    try:
        await telemetry_ping.send(
            ping, transport=telemetry_wiring.http_transport, debug=IDENTITY.debug_requested(env)
        )
    except Exception:
        logger.debug("telemetry: the install ping failed", exc_info=True)


def _spawn(coroutine: Coroutine[object, object, object]) -> None:
    """Run in the background, keeping a reference so the task is not collected midway."""
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


def build(settings: Settings, env: Mapping[str, str]) -> MCPServer:
    """Wire the engine, queue, supervisor, jobs and telemetry together."""
    supervisor = build_supervisor(settings)

    # Nothing is collected or sent until the first-run notice has been shown: that keeps
    # "shown before anything is sent" literal, and keeps the first ping an install,
    # which the ping decides from the consent file that collecting would create.
    notice_due = telemetry_notice.due(env, IDENTITY, host_platform=sys.platform)
    ping = None
    telemetry = None
    # A build whose versions are not release versions would have every request refused.
    shippable = telemetry_compose.versions_shippable(VERSION)
    if not shippable:
        logger.info("telemetry: %s is not a release build; nothing will be sent", VERSION)
    if not notice_due:
        # Decided before `compose.build`, which may create the consent file it reads.
        if shippable:
            ping = telemetry_ping.due(
                env,
                IDENTITY,
                plugin_version=VERSION,
                host_platform=sys.platform,
                ingest_url=IDENTITY.ingest_url(env),
            )
        telemetry = telemetry_compose.build(env, settings=settings, plugin_version=VERSION)
    notice = Notice(
        pending=notice_due,
        mark_shown=lambda: telemetry_notice.mark_shown(env, IDENTITY, host_platform=sys.platform),
        still_due=lambda: telemetry_notice.due(env, IDENTITY, host_platform=sys.platform),
    )

    def start_speech_server() -> None:
        """Kick off a cold start without waiting: this sentence goes out through `say`."""
        if supervisor is not None:
            _spawn(supervisor.ensure_running())

    engine = build_engine(settings.speech, settings.server, on_unavailable=start_speech_server)

    def on_finish(outcome: UtteranceOutcome) -> None:
        narration.finished(outcome)

    queue = SpeechQueue(
        prepare=engine.prepare,
        on_finish=on_finish,
        speaker=SpeakerLock(shared_dir=settings.server.shared_dir),
    )
    # Liveness only: the transcription client is the one that can ask the shared server
    # whether it is up, which is what `server_warm` records.
    probe = NemoTranscriber(base_url=settings.server.url, api_key=settings.server.api_key)

    speech = Speech(
        liveness=probe.liveness,
        voices=NemoSynthesizer(
            base_url=settings.server.url,
            api_key=settings.server.api_key,
            model=settings.server.tts_model,
        ).list_voices,
    )

    async def server_ready() -> bool:
        return await probe.liveness() is Liveness.READY

    narration = NarrationTracker(
        telemetry=telemetry,
        engine=engine.name,
        model=settings.server.tts_model if engine.name == "nemo" else None,
        voice=settings.speech.voice or engine.default_voice,
        pending=lambda: queue.stats().size,
        warm=server_ready if engine.name == "nemo" else None,
    )

    def schedule_sends() -> None:
        """Deferred to the server's lifespan: `build()` runs before an event loop exists.
        The ping goes whatever the telemetry mode; the backlog only when it collects."""
        if ping is not None:
            _spawn(_send_ping(ping, env))
        if telemetry is not None and shippable:
            _spawn(_ship_backlog(env, settings))

    desk = build_desk(settings)
    # A worker that died while no session was asking is found now, not at the first
    # status call, and the lock it left is freed for this session's first job.
    try:
        sweep(desk)
    except OSError:
        logger.warning("could not reconcile earlier jobs at start", exc_info=True)
    return create_server(
        settings=settings,
        engine=engine,
        queue=queue,
        supervisor=supervisor,
        desk=desk,
        speech=speech,
        telemetry=telemetry,
        narration=narration,
        notice=notice,
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
    env = dict(os.environ)
    settings = Settings.from_env(env)
    try:
        server = build(settings, env)
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error
    run(server)


if __name__ == "__main__":
    main()
