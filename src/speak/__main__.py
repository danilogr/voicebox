"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os
import sys

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
        # No endpoint is configured yet (spec 11's rollout still needs a domain);
        # `ship` itself no-ops on the network step but still runs the debug branch.
        url = env.get("SPEAK_TELEMETRY_ENDPOINT") or DEFAULT_TELEMETRY_ENDPOINT
        shipped = await telemetry_ship.ship(conn, url=url, transport=_http_transport, debug=debug)
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
        on_session_start=schedule_ship,
    )


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
