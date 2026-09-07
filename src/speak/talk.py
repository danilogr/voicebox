"""The /talk entry point: record the user, transcribe, print the transcript.

stdout is this program's product - a slash command injects it into the prompt -
so unlike the library modules, printing here is the point.
"""

import asyncio
import logging
import os
import sys

from speak.capture import MicrophoneRecorder, Recorder, RecorderUnavailable
from speak.config import Settings
from speak.engines.asr import NemoTranscriber, Transcriber, TranscriptionFailed
from speak.miclock import MicLock, MicrophoneBusy
from speak.runtime import capture_limits, configure_logging, health_probe
from speak.supervisor import NemoSupervisor, process_is_alive
from speak.tools import LISTEN_OFF, NO_ASR

logger = logging.getLogger(__name__)


async def capture(
    settings: Settings,
    *,
    recorder: Recorder,
    transcriber: Transcriber,
    lock: MicLock,
) -> str:
    """Record one utterance and return its transcript, or "" for silence."""
    if not await transcriber.has_asr():
        raise TranscriptionFailed(NO_ASR)

    lock.acquire()
    try:
        recording = await recorder.record(capture_limits(settings))
        if not recording.has_speech:
            return ""
        return await transcriber.transcribe(recording.wav, language=settings.listen_language)
    finally:
        lock.release()


async def _run(settings: Settings) -> str:
    """Make sure a server is up, then capture against it."""
    supervisor = NemoSupervisor(
        state_dir=settings.state_dir,
        port=settings.nemo_port,
        binary=settings.nemo_binary,
        model=settings.nemo_model,
        asr_model=settings.nemo_asr_model,
        seed=settings.seed,
        probe=health_probe(settings.nemo_url),
    )
    transcriber = NemoTranscriber(
        base_url=settings.nemo_url,
        api_key=settings.nemo_api_key,
        model=settings.nemo_asr_model,
    )

    # Register for this process's lifetime so a standalone /talk cannot leave a
    # gigabyte of speech server running behind it. When a Claude Code session is
    # already holding the server, release is a no-op and the server stays warm.
    await supervisor.register()
    try:
        # Only a server that is not there at all gets started. One that is up but
        # was started without transcription must not be met with a second server
        # on the same port; `capture` reports that case instead.
        if not await transcriber.reachable():
            await supervisor.ensure_running()
        return await capture(
            settings,
            recorder=MicrophoneRecorder(),
            transcriber=transcriber,
            lock=MicLock(state_dir=settings.state_dir, is_alive=process_is_alive),
        )
    finally:
        await supervisor.release()


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    if not settings.listen:
        print(f"speak: {LISTEN_OFF}", file=sys.stderr)
        raise SystemExit(1)
    try:
        text = asyncio.run(_run(settings))
    except (MicrophoneBusy, RecorderUnavailable, TranscriptionFailed) as error:
        print(f"speak: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(text if text else "(heard nothing)")
