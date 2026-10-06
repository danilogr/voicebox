"""The MCP server: the speech tools, and `listen` when voice input is on."""

import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from speak import tools
from speak.capture import Recorder, RecorderUnavailable, Recording, TrackerSettings, describe
from speak.cue import Cue
from speak.engines.asr import Liveness, Transcriber, TranscriptionFailed
from speak.engines.base import Engine
from speak.miclock import MicLock, MicrophoneBusy
from speak.queue import QueueStats
from speak.queue import describe as describe_queue
from speak.settings import Settings
from speak.telemetry.session import TelemetrySession
from speak.telemetry.wiring import normalize_harness
from speak.text import normalize
from speak.transcribe import service as transcription

logger = logging.getLogger(__name__)

VERSION = "1.6.0"

# The queue decides when it is wedged, because only it can see its own progress. This
# is the outer guard: a Queue implementation that neither finishes nor raises would
# otherwise hold a tool call open for the life of the session.
DRAIN_BACKSTOP = 600.0

# Claude Code reads `commands/*.md` on its own. Every other MCP client sees only what
# this server offers, so conversation mode is served from that same file as a prompt:
# one copy of the instruction, whichever app is asking for it.
CONVERSE_COMMAND = Path(__file__).resolve().parents[2] / "commands" / "converse.md"

# Long enough to cover a cold start (see harness/performance.md); `listen` waits it
# out rather than reporting a server that this very session is still starting.
WARMUP_TIMEOUT = 20.0


class Queue(Protocol):
    def enqueue(
        self, text: str, *, voice: str | None, rate: float | None, intent: str | None = None
    ) -> int: ...

    async def stop(self) -> int: ...

    async def run(self) -> None: ...

    async def drain(self) -> None: ...

    def stats(self) -> QueueStats: ...


class Supervisor(Protocol):
    async def register(self) -> None: ...

    async def release(self) -> bool: ...

    async def ensure_running(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Ears:
    """Everything the `listen` tool needs. Absent when voice input is off."""

    recorder: Recorder
    transcriber: Transcriber
    mic_lock: MicLock
    limits: TrackerSettings
    cue: Cue


@dataclass(slots=True)
class SessionState:
    """Voice and rate for this connection only; never written to disk."""

    voice: str | None = None
    rate: float | None = None
    registered: bool = False
    randomize: bool = False
    warmup: "asyncio.Task[bool] | None" = None
    # Set once the first tool call has looked for the MCP client's own `client_info`
    # (spec 2, tier 1), whether or not one was actually found - there is nothing more
    # to learn from asking again for the life of the connection.
    harness_checked: bool = False


def choose_pool(
    configured: Sequence[str],
    engine_pool: Sequence[str] | None,
    reported: Sequence[str],
) -> list[str]:
    """Decide which voices a random pick may draw from.

    A configured pool is taken at its word, so it still works with an engine that
    reports no names. An engine's own pool is narrowed to what is actually
    installed, and falls back to everything reported when it matches nothing.
    """
    if configured:
        return list(configured)
    if engine_pool:
        installed = [name for name in engine_pool if name in reported]
        if installed:
            return installed
    return list(reported)


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def client_info(ctx: Context) -> tuple[str, str | None] | None:
    """(name, version) from the MCP client's own `client_info`, or `None`.

    `None` covers two real cases alike: `ctx.session.client_params` is `None`
    when a host declares no client info at all (optional in the MCP spec since
    2026-07-28), and `ctx.session` itself raises outside of a live request - which
    every test that calls a tool directly, with no client ever connected, does.
    Either way there is nothing to learn, and the caller falls back to whatever
    the environment already decided at session start.
    """
    try:
        params = ctx.session.client_params
    except Exception:
        return None
    if params is None:
        return None
    return params.client_info.name, params.client_info.version


async def shutdown(
    *,
    queue: Queue,
    worker: asyncio.Task[None],
    supervisor: "Supervisor | None",
    session: "SessionState",
    telemetry: "TelemetrySession | None" = None,
) -> None:
    """Silence the speaker first, then tear the session down.

    Stopping the queue before cancelling the worker matters: the worker owns the
    running child process, so cancelling it first would drop the handle and leave
    the machine talking after the session is gone.
    """
    await queue.stop()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    if supervisor is not None and session.registered:
        await supervisor.release()
    if telemetry is not None:
        telemetry.close()
        telemetry.conn.close()


async def warm_speech_server(supervisor: "Supervisor", transcriber: Transcriber) -> bool:
    """Start the shared speech server, unless something already holds its port.

    `ensure_running` spawns unconditionally: its lock only excludes another session
    starting at the same moment, so a session opened later would spawn a second
    server, overwrite the record of the first, and leave it orphaned — the last
    session out would then stop the wrong process. Liveness, not readiness: any
    HTTP answer means the port is taken and this session has nothing to start.
    """
    if await transcriber.reachable():
        return False
    return await supervisor.ensure_running()


async def start_listening(
    *,
    supervisor: "Supervisor | None",
    session: "SessionState",
    ears: "Ears | None",
) -> asyncio.Task[bool] | None:
    """Claim the shared speech server for a session that can listen.

    `/talk` runs as its own short-lived process, so it needs a server that is
    already warm. Registering is awaited; the warmup is handed back as a task the
    caller owns, so neither the liveness check nor a cold start delays the session
    opening. It is also recorded on the session, because the first `listen` of a
    session can arrive while that same warmup is still loading models and has to
    wait for it rather than report the server missing.
    """
    if ears is None or supervisor is None:
        return None
    session.registered = True
    await supervisor.register()
    session.warmup = asyncio.create_task(warm_speech_server(supervisor, ears.transcriber))
    return session.warmup


async def settle(warmup: "asyncio.Task[bool] | None") -> None:
    """Give this session's own cold start time to finish before judging the server.

    `asyncio.wait` rather than `wait_for`, which cancels what it is waiting on when
    it times out — and the warmup is the thing that will make the next call work.
    Timing out here is not an error: the caller goes on to ask the server directly,
    which answers "still starting" if that is what it is.
    """
    if warmup is None or warmup.done():
        return
    await asyncio.wait({warmup}, timeout=WARMUP_TIMEOUT)


def split_frontmatter(text: str) -> tuple[str, str]:
    """A command file's `description` and its body, with the frontmatter removed.

    Only `description` is read, because it is the only key a prompt has a place for.
    A block that never closes is left in the body: stripping to the end of the file
    would serve an empty instruction.
    """
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    description = ""
    for line in text[4:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "description":
            description = value.strip()
    return description, text[end + 5 :].lstrip("\n")


def create_server(
    *,
    settings: Settings,
    engine: Engine,
    queue: Queue,
    supervisor: Supervisor | None,
    rng: random.Random | None = None,
    ears: Ears | None = None,
    telemetry: TelemetrySession | None = None,
    scribe: "transcription.Scribe | None" = None,
    on_session_start: Callable[[], None] | None = None,
) -> MCPServer:
    session = SessionState(
        voice=settings.speech.voice,
        rate=settings.speech.rate,
        randomize=settings.speech.randomize_voice,
    )
    roll = rng or random.Random()

    def note_heard(
        status: str,
        *,
        transcript: str | None = None,
        device_name: str | None = None,
        asr_ms: int | None = None,
        wav: bytes | None = None,
    ) -> None:
        if telemetry is not None:
            telemetry.heard(
                source="listen",
                status=status,
                transcript=transcript,
                device_name=device_name,
                asr_ms=asr_ms,
                wav=wav,
            )

    def note_harness(ctx: Context) -> None:
        """Correct the session row's harness from the MCP client's own
        `client_info`, the first time any tool call can supply it.

        Only ever narrows toward tier 1 (spec 2): when `client_info` is not
        there - no live request, or a host that declared none - the row keeps
        whatever the environment already decided at session start, rather than
        being overwritten with a guess.
        """
        if telemetry is None or session.harness_checked:
            return
        session.harness_checked = True
        found = client_info(ctx)
        if found is None:
            return
        name, version = found
        telemetry.update_harness(normalize_harness(name), version)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        worker = asyncio.create_task(queue.run())
        if on_session_start is not None:
            # Only here is a loop guaranteed to be running: `build()` calls this
            # factory before `server.run()` starts one, so anything scheduling a
            # background task (shipping the telemetry backlog) has to wait for it.
            on_session_start()
        warmup = await start_listening(supervisor=supervisor, session=session, ears=ears)
        try:
            yield
        finally:
            if warmup is not None:
                warmup.cancel()
                await asyncio.gather(warmup, return_exceptions=True)
            await shutdown(
                queue=queue,
                worker=worker,
                supervisor=supervisor,
                session=session,
                telemetry=telemetry,
            )

    server = MCPServer(name="speak", version=VERSION, lifespan=lifespan)

    async def session_voice() -> str | None:
        """Resolve the session voice, rolling one the first time it is needed.

        Resolution is lazy because a speech server may report no voices until it
        has warmed up. An unresolved voice means this utterance uses the engine
        default and the roll is retried on the next one.
        """
        if session.voice is not None or not session.randomize:
            return session.voice
        pool = choose_pool(
            settings.speech.voice_pool, engine.random_pool, await engine.list_voices()
        )
        if not pool:
            return None
        session.voice = roll.choice(pool)
        logger.info("voice for this session: %s", session.voice)
        return session.voice

    @server.tool(name="speak", description=tools.SPEAK)
    async def speak(
        text: Annotated[str, Field(description=tools.TEXT)],
        voice: Annotated[str | None, Field(description=tools.VOICE_OVERRIDE)] = None,
        rate: Annotated[float | None, Field(description=tools.RATE)] = None,
        intent: Annotated[tools.Intent | None, Field(description=tools.INTENT)] = None,
        *,
        ctx: Context,
    ) -> str:
        note_harness(ctx)
        try:
            spoken = normalize(text, max_chars=settings.speech.max_chars)
        except ValueError as error:
            raise ToolError(str(error)) from error

        if supervisor is not None and not session.registered:
            session.registered = True
            await supervisor.register()

        ahead = queue.enqueue(
            spoken,
            voice=voice if voice is not None else await session_voice(),
            rate=rate if rate is not None else session.rate,
            intent=intent,
        )
        if ahead == 0:
            return f'speaking: "{spoken}"'
        return f'queued behind {ahead} utterance{_plural(ahead)}: "{spoken}"'

    @server.tool(name="list_voices", description=tools.LIST_VOICES)
    async def list_voices() -> str:
        voices = await engine.list_voices()
        active = await session_voice() or engine.default_voice or "the engine default"
        if not voices:
            return (
                f"engine: {engine.name}; active voice: {active}. "
                "This engine does not report voice names."
            )
        return f"engine: {engine.name}; active voice: {active}. Available: {', '.join(voices)}."

    @server.tool(name="set_voice", description=tools.SET_VOICE)
    async def set_voice(
        voice: Annotated[str, Field(description=tools.VOICE_NAME)],
    ) -> str:
        wanted = voice.strip()
        if not wanted:
            raise ToolError("set_voice needs a voice name")

        voices = await engine.list_voices()
        match = next((v for v in voices if v.lower() == wanted.lower()), None)
        if voices and match is None:
            raise ToolError(f'unknown voice "{wanted}"; available: {", ".join(voices)}')

        session.voice = match or wanted
        session.randomize = False
        return f"voice set to {session.voice} for this session"

    @server.tool(name="stop_speaking", description=tools.STOP_SPEAKING)
    async def stop_speaking() -> str:
        dropped = await queue.stop()
        return f"stopped; dropped {dropped} utterance{_plural(dropped)}"

    @server.tool(name="speech_status", description=tools.SPEECH_STATUS)
    async def speech_status() -> str:
        stats = queue.stats()
        if stats.size == 0:
            return f"nothing queued; {stats.finished} spoken so far"
        return f"{describe_queue(stats)}; {stats.finished} spoken so far"

    if scribe is not None:
        # The transcription lock is claimed for a worker that does not exist yet, so
        # between this call returning and that worker taking the lock over the record
        # still names this process; without this second gate a concurrent pair - which
        # is what "transcribe these three recordings" produces - would interleave in
        # that window. Refusing rather than queueing keeps the answer immediate: the
        # call that loses the race gets the busy message from the lock itself.
        transcribing = asyncio.Lock()

        @server.tool(name="transcribe", description=tools.TRANSCRIBE)
        async def transcribe(
            path: Annotated[str, Field(description=tools.TRANSCRIBE_PATH)],
            output_dir: Annotated[
                str | None, Field(description=tools.TRANSCRIBE_OUTPUT_DIR)
            ] = None,
            format: Annotated[
                tools.TranscriptFormat, Field(description=tools.TRANSCRIBE_FORMAT)
            ] = "text",
            diarize: Annotated[bool, Field(description=tools.TRANSCRIBE_DIARIZE)] = True,
            language: Annotated[str | None, Field(description=tools.TRANSCRIBE_LANGUAGE)] = None,
            translate_to: Annotated[
                str | None, Field(description=tools.TRANSCRIBE_TRANSLATE)
            ] = None,
            *,
            ctx: Context,
        ) -> str:
            note_harness(ctx)
            if transcribing.locked():
                return transcription.busy_message(scribe)
            async with transcribing:
                return await transcription.start(
                    scribe,
                    settings,
                    transcription.TranscribeRequest(
                        path=path,
                        output_dir=output_dir,
                        format=format,
                        diarize=diarize,
                        language=language,
                        translate_to=translate_to,
                    ),
                    telemetry,
                )

        @server.tool(name="transcription_status", description=tools.TRANSCRIPTION_STATUS)
        async def transcription_status(
            job_id: Annotated[str | None, Field(description=tools.TRANSCRIPTION_JOB_ID)] = None,
            *,
            ctx: Context,
        ) -> str:
            note_harness(ctx)
            return transcription.status(scribe, settings, job_id)

    if ears is not None:
        # The microphone lock is re-entrant by pid, so it separates sessions but not
        # two calls inside this one process; without this second gate a concurrent
        # pair would open the microphone together and release the lock out from under
        # each other. Refusing rather than queueing keeps the answer immediate.
        # Offered only alongside `listen`: the instruction is to call it, and a model
        # told to use a tool that is not there is worse off than one never told.
        try:
            description, body = split_frontmatter(CONVERSE_COMMAND.read_text(encoding="utf-8"))
        except OSError as error:
            logger.warning("conversation mode is not offered as a prompt: %s", error)
        else:

            @server.prompt(name="converse", description=description)
            def converse() -> str:
                return body

        listening = asyncio.Lock()

        @server.tool(name="listen", description=tools.LISTEN)
        async def listen(
            max_seconds: Annotated[
                float | None, Field(description=tools.LISTEN_SECONDS, gt=0, allow_inf_nan=False)
            ] = None,
            *,
            ctx: Context,
        ) -> str:
            note_harness(ctx)
            if listening.locked():
                note_heard("refused")
                raise ToolError(tools.ALREADY_LISTENING)

            async with listening:
                try:
                    await asyncio.wait_for(queue.drain(), timeout=DRAIN_BACKSTOP)
                except TimeoutError as error:
                    # Recording anyway puts this session's own voice where the user's
                    # answer belongs, and the model acts on it as a request. An honest
                    # refusal can be retried; a fabricated answer cannot be taken back.
                    pending = describe_queue(queue.stats())
                    logger.warning("speech queue is wedged; refusing to record over it")
                    note_heard("refused")
                    raise ToolError(tools.STILL_SPEAKING.format(pending=pending)) from error

                await settle(session.warmup)

                # Liveness first: `has_asr` answers False for any HTTP failure, so a
                # server that is not there at all would otherwise be reported as one
                # running without a transcription model, and neither remedy that
                # message offers would apply. A server that is up but still loading
                # its models gets its own answer for the same reason: "wait" is the
                # only correct advice, and neither other message gives it.
                state = await ears.transcriber.liveness()
                if state is Liveness.ABSENT:
                    note_heard("failed")
                    raise ToolError(tools.SERVER_UNREACHABLE)
                if state is Liveness.STARTING:
                    note_heard("failed")
                    raise ToolError(tools.SERVER_STARTING)
                if not await ears.transcriber.has_asr():
                    note_heard("failed")
                    raise ToolError(tools.NO_ASR)

                try:
                    ears.mic_lock.acquire()
                except MicrophoneBusy as error:
                    note_heard("refused")
                    raise ToolError(str(error)) from error

                recording: Recording | None = None
                try:
                    # The cues bracket the recording and nothing else: they say the
                    # microphone is open and that it has shut, which is the moment
                    # recording stops rather than the moment the transcript lands.
                    async with ears.cue.around_recording() as outcome:
                        recording = await ears.recorder.record(_limits(ears.limits, max_seconds))
                        outcome.heard = recording.has_speech
                    logger.info("capture: %s", describe(recording))
                    if not recording.has_speech:
                        note_heard("heard_nothing", device_name=recording.device, wav=recording.wav)
                        return tools.HEARD_NOTHING
                    started = time.monotonic()
                    heard = await ears.transcriber.transcribe(
                        recording.wav, language=settings.listen_language
                    )
                    asr_ms = round((time.monotonic() - started) * 1000)
                except (TranscriptionFailed, RecorderUnavailable) as error:
                    note_heard(
                        "failed",
                        device_name=recording.device if recording else None,
                        wav=recording.wav if recording else None,
                    )
                    raise ToolError(str(error)) from error
                finally:
                    ears.mic_lock.release()

                note_heard(
                    "ok" if heard else "heard_nothing",
                    transcript=heard,
                    device_name=recording.device,
                    asr_ms=asr_ms,
                    wav=recording.wav,
                )
                return heard or tools.HEARD_NOTHING

    return server


def _limits(configured: TrackerSettings, max_seconds: float | None) -> TrackerSettings:
    """Apply the caller's cap, never above the configured one.

    Non-finite values are rejected here as well as in the schema, the way
    `config._number` already rejects them: `nan` passes every `<= 0` test and `inf`
    caps nothing, so either one records until the process dies while holding the
    microphone lock against every other session. The configured cap is a ceiling
    rather than a suggestion, so a larger override is clamped to it.
    """
    if max_seconds is None or not math.isfinite(max_seconds) or max_seconds <= 0:
        return configured
    return replace(configured, max_ms=min(max_seconds * 1000, configured.max_ms))
