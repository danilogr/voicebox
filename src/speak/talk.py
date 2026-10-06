"""The /talk entry point: record the user, transcribe, print the transcript.

stdout is this program's product - a slash command injects it into the prompt -
so unlike the library modules, printing here is the point. Whatever lands there is
read as the user's own words, so a failure leaves as one actionable line marked as
one, on both streams, with a non-zero exit and never a traceback.
"""

import asyncio
import logging
import os
import sys
import time
from typing import NoReturn

from speak_core.engines.asr import Liveness, NemoTranscriber, Transcriber, TranscriptionFailed
from speak_core.runtime import configure_logging, supervisor_for
from speak_core.supervisor import NemoSupervisor, process_is_alive
from speak_core.telemetry import ping as telemetry_ping
from speak_core.telemetry import wiring as telemetry_wiring

from speak.capture import Recorder, RecorderUnavailable, Recording, describe
from speak.cue import Cue
from speak.listening import build_cue, build_recorder, capture_limits, transcription_timeout
from speak.miclock import MicLock, MicrophoneBusy
from speak.server import VERSION
from speak.settings import PLUGIN, Settings
from speak.telemetry import compose as telemetry_compose
from speak.telemetry.contract import IDENTITY
from speak.telemetry.session import TelemetrySession
from speak.tools import (
    NO_ASR,
    NO_SERVER,
    SERVER_STARTING,
    SERVER_UNREACHABLE,
    TALK_HEARD_NOTHING,
)

logger = logging.getLogger(__name__)


def _note(
    telemetry: TelemetrySession | None,
    status: str,
    *,
    transcript: str | None = None,
    device_name: str | None = None,
    asr_ms: int | None = None,
    wav: bytes | None = None,
) -> None:
    if telemetry is not None:
        telemetry.heard(
            source="talk",
            status=status,
            transcript=transcript,
            device_name=device_name,
            asr_ms=asr_ms,
            wav=wav,
        )


async def capture(
    settings: Settings,
    *,
    recorder: Recorder,
    transcriber: Transcriber,
    lock: MicLock,
    cue: Cue,
    telemetry: TelemetrySession | None = None,
) -> str:
    """Record one utterance and return its transcript, or "" for silence."""
    # Liveness before capability, the way `speak.server.listen` does it. `has_asr`
    # answers False for any HTTP failure, so a server that is absent, or one this
    # process started eight seconds ago and is still loading, would be reported as a
    # server running without a transcription model — and both remedies that message
    # names, reinstalling the model and restarting another session, are wrong.
    state = await transcriber.liveness()
    if state is Liveness.ABSENT:
        _note(telemetry, "failed")
        raise TranscriptionFailed(SERVER_UNREACHABLE)
    if state is Liveness.STARTING:
        _note(telemetry, "failed")
        raise TranscriptionFailed(SERVER_STARTING)
    if not await transcriber.has_asr():
        _note(telemetry, "failed")
        raise TranscriptionFailed(NO_ASR)

    try:
        lock.acquire()
    except MicrophoneBusy:
        _note(telemetry, "refused")
        raise
    except OSError:
        _note(telemetry, "failed")
        raise

    recording: Recording | None = None
    try:
        # Cued only once the microphone is this process's to open, and cued shut when
        # recording stops rather than when the transcript comes back.
        async with cue.around_recording() as outcome:
            recording = await recorder.record(capture_limits(settings))
            # The closing sound is the only report an empty capture gets before the
            # transcript would have arrived, so the outcome has to be set inside the
            # bracket rather than acted on after it.
            outcome.heard = recording.has_speech
        logger.info("capture: %s", describe(recording))
        if not recording.has_speech:
            _note(telemetry, "heard_nothing", device_name=recording.device, wav=recording.wav)
            return ""
        started = time.monotonic()
        text = await transcriber.transcribe(recording.wav, language=settings.listen_language)
    except (TranscriptionFailed, RecorderUnavailable, OSError):
        _note(
            telemetry,
            "failed",
            device_name=recording.device if recording else None,
            wav=recording.wav if recording else None,
        )
        raise
    finally:
        lock.release()

    asr_ms = round((time.monotonic() - started) * 1000)
    _note(
        telemetry,
        "ok" if text else "heard_nothing",
        transcript=text,
        device_name=recording.device,
        asr_ms=asr_ms,
        wav=recording.wav,
    )
    return text


async def dictate(
    settings: Settings,
    *,
    supervisor: NemoSupervisor,
    recorder: Recorder,
    transcriber: Transcriber,
    lock: MicLock,
    cue: Cue,
    telemetry: TelemetrySession | None = None,
) -> str:
    """Hold the shared speech server for this process, then capture against it.

    Every way this attempt can fail before `capture` takes over writes its `failed`
    row here; `capture` writes its own, so each attempt is recorded exactly once.
    """
    # Register for this process's lifetime so a standalone /talk cannot leave a
    # gigabyte of speech server running behind it. When a Claude Code session is
    # already holding the server, release is a no-op and the server stays warm.
    try:
        await supervisor.register()
    except OSError:
        _note(telemetry, "failed")
        raise
    try:
        try:
            await _ensure_server(settings, supervisor, transcriber)
        except (TranscriptionFailed, OSError):
            _note(telemetry, "failed")
            raise
        return await capture(
            settings,
            recorder=recorder,
            transcriber=transcriber,
            lock=lock,
            cue=cue,
            telemetry=telemetry,
        )
    finally:
        await supervisor.release()


async def _ensure_server(
    settings: Settings, supervisor: NemoSupervisor, transcriber: Transcriber
) -> None:
    """Start the speech server unless something already answers on its port.

    `ensure_running` spawns unconditionally: its lock only excludes another session
    starting at the same moment. So a server that is already up must not be met with
    a second one, which would overwrite the record of the first and leave the last
    process out stopping the wrong pid. Liveness, not readiness - any HTTP answer
    means the port is taken. A server that is up but has no transcription model is
    likewise not replaced; `capture` reports that case instead.
    `speak.server.warm_speech_server` guards the session path the same way for the
    same reason: change the two together.
    """
    if await transcriber.reachable():
        return
    if not settings.server.autostart:
        # `__main__.build_supervisor` builds no supervisor under this setting; a user
        # who said they would run the server must not get one spawned behind their
        # back here either.
        raise TranscriptionFailed(SERVER_UNREACHABLE)
    await _start(supervisor)


async def _start(supervisor: NemoSupervisor) -> None:
    """Start the server, turning a missing binary into an error that says what to do."""
    try:
        await supervisor.ensure_running()
    except OSError as error:
        raise TranscriptionFailed(f"{NO_SERVER} ({error})") from error


async def _run(settings: Settings) -> str:
    """Build the real collaborators and dictate against them.

    `/talk` is its own short-lived process rather than an MCP session, so it gets
    its own session row - and never ships: shipping is a network call, and this
    process's whole job is to print a transcript as fast as it can. Rows it writes
    are shipped by the next `speak-mcp` session to start, since both read the same
    local store.

    The install ping is the one request it may make, because someone who only ever
    dictates would otherwise never be counted. It runs alongside the recording and
    is abandoned, not awaited, if it has not finished when the transcript is ready;
    an abandoned ping is due again next time.
    """
    # Decided before `telemetry_compose.build`, which may create the consent file this
    # reads to tell an install from an upgrade.
    ping = telemetry_ping.due(
        os.environ,
        IDENTITY,
        plugin_version=VERSION,
        host_platform=sys.platform,
        ingest_url=IDENTITY.ingest_url(os.environ),
    )
    telemetry = telemetry_compose.build(os.environ, settings=settings, plugin_version=VERSION)
    pinging = (
        asyncio.create_task(
            telemetry_ping.send(
                ping,
                transport=telemetry_wiring.http_transport,
                debug=IDENTITY.debug_requested(os.environ),
            )
        )
        if ping is not None
        else None
    )
    try:
        return await dictate(
            settings,
            supervisor=supervisor_for(settings.server),
            recorder=build_recorder(settings),
            transcriber=NemoTranscriber(
                base_url=settings.server.url,
                api_key=settings.server.api_key,
                model=settings.server.asr_model,
                timeout=transcription_timeout(settings),
            ),
            lock=MicLock(state_dir=settings.state_dir, is_alive=process_is_alive),
            cue=build_cue(settings),
            telemetry=telemetry,
        )
    finally:
        if telemetry is not None:
            telemetry.close()
            telemetry.conn.close()
        if pinging is not None:
            pinging.cancel()
            await asyncio.gather(pinging, return_exceptions=True)


def _fail(message: str) -> NoReturn:
    """Report one line, on both streams, and stop.

    `commands/talk.md` tells Claude that a line beginning `speak:` is an error
    rather than the user's words, and it has to arrive for that to help. Whether a
    slash command's `!` injection carries stderr from a command that exited non-zero
    is not something this plugin controls, and if it does not, stderr alone leaves
    the model an empty transcript under an instruction to act on it as the user's
    request. The prefix is what keeps the stdout copy from being read as speech.
    """
    line = f"speak: {message}"
    print(line, file=sys.stderr)
    print(line)
    raise SystemExit(1)


def main() -> None:
    configure_logging(PLUGIN)
    settings = Settings.from_env(os.environ)
    try:
        text = asyncio.run(_run(settings))
    except (MicrophoneBusy, RecorderUnavailable, TranscriptionFailed, OSError) as error:
        # OSError is in the list because the state directory and the microphone are
        # both filesystem-backed: an unwritable one would otherwise print a
        # traceback into the prompt as if the user had typed it.
        _fail(str(error))
    print(text or TALK_HEARD_NOTHING)
