"""The /talk entry point: record the user, transcribe, print the transcript.

stdout is this program's product - a slash command injects it into the prompt -
so unlike the library modules, printing here is the point. It is also why nothing
but the transcript may reach stdout: whatever lands there is read as the user's
own words, so a failure leaves as one actionable line on stderr and a non-zero
exit rather than a traceback.
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
from speak.tools import LISTEN_OFF, NO_ASR, NO_SERVER, SERVER_UNREACHABLE, TALK_HEARD_NOTHING

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


async def dictate(
    settings: Settings,
    *,
    supervisor: NemoSupervisor,
    recorder: Recorder,
    transcriber: Transcriber,
    lock: MicLock,
) -> str:
    """Hold the shared speech server for this process, then capture against it."""
    # Register for this process's lifetime so a standalone /talk cannot leave a
    # gigabyte of speech server running behind it. When a Claude Code session is
    # already holding the server, release is a no-op and the server stays warm.
    await supervisor.register()
    try:
        # `ensure_running` spawns unconditionally: its lock only excludes another
        # session starting at the same moment. So a server that is already up must
        # not be met with a second one, which would overwrite the record of the
        # first and leave the last process out stopping the wrong pid. Liveness,
        # not readiness - any HTTP answer means the port is taken. A server that is
        # up but has no transcription model is likewise not replaced; `capture`
        # reports that case instead. `speak.server.warm_speech_server` guards the
        # session path the same way for the same reason: change the two together.
        if not await transcriber.reachable():
            if not settings.nemo_autostart:
                # `__main__.build_supervisor` builds no supervisor under this setting;
                # a user who said they would run the server must not get one spawned
                # behind their back here either.
                raise TranscriptionFailed(SERVER_UNREACHABLE)
            await _start(supervisor)
        return await capture(settings, recorder=recorder, transcriber=transcriber, lock=lock)
    finally:
        await supervisor.release()


async def _start(supervisor: NemoSupervisor) -> None:
    """Start the server, turning a missing binary into an error that says what to do."""
    try:
        await supervisor.ensure_running()
    except OSError as error:
        raise TranscriptionFailed(f"{NO_SERVER} ({error})") from error


async def _run(settings: Settings) -> str:
    """Build the real collaborators and dictate against them."""
    return await dictate(
        settings,
        supervisor=NemoSupervisor(
            state_dir=settings.state_dir,
            port=settings.nemo_port,
            binary=settings.nemo_binary,
            model=settings.nemo_model,
            asr_model=settings.nemo_asr_model,
            seed=settings.seed,
            probe=health_probe(settings.nemo_url),
        ),
        recorder=MicrophoneRecorder(),
        transcriber=NemoTranscriber(
            base_url=settings.nemo_url,
            api_key=settings.nemo_api_key,
            model=settings.nemo_asr_model,
        ),
        lock=MicLock(state_dir=settings.state_dir, is_alive=process_is_alive),
    )


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    if not settings.listen:
        print(f"speak: {LISTEN_OFF}", file=sys.stderr)
        raise SystemExit(1)
    try:
        text = asyncio.run(_run(settings))
    except (MicrophoneBusy, RecorderUnavailable, TranscriptionFailed, OSError) as error:
        # OSError is in the list because the state directory and the microphone are
        # both filesystem-backed: an unwritable one would otherwise print a
        # traceback into the prompt as if the user had typed it.
        print(f"speak: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(text or TALK_HEARD_NOTHING)
