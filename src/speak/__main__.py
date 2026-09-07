"""Entry point: build everything from the environment and serve on stdio."""

import asyncio
import logging
import os

from mcp.server.mcpserver import MCPServer

from speak.capture import MicrophoneRecorder
from speak.config import Settings
from speak.engines import build_engine
from speak.engines.asr import NemoTranscriber
from speak.miclock import MicLock
from speak.queue import SpeechQueue
from speak.runtime import capture_limits, configure_logging, health_probe
from speak.server import Ears, create_server
from speak.supervisor import NemoSupervisor, process_is_alive

logger = logging.getLogger(__name__)

_background: set[asyncio.Task[object]] = set()


def build_ears(settings: Settings) -> Ears | None:
    """Everything the listen tool needs, or None when voice input is off."""
    if not settings.listen:
        return None
    return Ears(
        recorder=MicrophoneRecorder(),
        transcriber=NemoTranscriber(
            base_url=settings.nemo_url,
            api_key=settings.nemo_api_key,
            model=settings.nemo_asr_model,
        ),
        mic_lock=MicLock(state_dir=settings.state_dir, is_alive=process_is_alive),
        limits=capture_limits(settings),
    )


def build_supervisor(settings: Settings) -> NemoSupervisor | None:
    """The shared speech server this session may start, or None when it must not.

    Listening needs the server whatever the speech engine is. The transcription
    model is served only when this session enables listening, so a session that
    never listens cannot be broken by a missing ASR model.
    """
    if not settings.nemo_autostart or not (settings.engine == "nemo" or settings.listen):
        return None
    return NemoSupervisor(
        state_dir=settings.state_dir,
        port=settings.nemo_port,
        binary=settings.nemo_binary,
        model=settings.nemo_model,
        asr_model=settings.nemo_asr_model if settings.listen else None,
        seed=settings.seed,
        probe=health_probe(settings.nemo_url),
    )


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
    queue = SpeechQueue(prepare=engine.prepare)
    return create_server(
        settings=settings,
        engine=engine,
        queue=queue,
        supervisor=supervisor,
        ears=build_ears(settings),
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
