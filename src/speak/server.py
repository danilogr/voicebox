"""The MCP server: the speech tools, and `listen` when voice input is on."""

import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Annotated, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from speak import tools
from speak.capture import Recorder, RecorderUnavailable, TrackerSettings, describe
from speak.config import Settings
from speak.cue import Cue
from speak.engines.asr import Liveness, Transcriber, TranscriptionFailed
from speak.engines.base import Engine
from speak.miclock import MicLock, MicrophoneBusy
from speak.text import normalize

logger = logging.getLogger(__name__)

VERSION = "1.0.0"

DRAIN_TIMEOUT = 60.0

# Long enough to cover a cold start, which is about ten seconds; `listen` waits it
# out rather than reporting a server that this very session is still starting.
WARMUP_TIMEOUT = 20.0


class Queue(Protocol):
    def enqueue(self, text: str, *, voice: str | None, rate: float | None) -> int: ...

    async def stop(self) -> int: ...

    async def run(self) -> None: ...

    async def drain(self) -> None: ...


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


async def shutdown(
    *,
    queue: Queue,
    worker: asyncio.Task[None],
    supervisor: "Supervisor | None",
    session: "SessionState",
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


def create_server(
    *,
    settings: Settings,
    engine: Engine,
    queue: Queue,
    supervisor: Supervisor | None,
    rng: random.Random | None = None,
    ears: Ears | None = None,
) -> MCPServer:
    session = SessionState(
        voice=settings.voice, rate=settings.rate, randomize=settings.randomize_voice
    )
    roll = rng or random.Random()
    # Voice input needs both the configuration and the wiring; settling that once
    # keeps the `listen` tool and the session warmup from disagreeing.
    if not settings.listen:
        ears = None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        worker = asyncio.create_task(queue.run())
        warmup = await start_listening(supervisor=supervisor, session=session, ears=ears)
        try:
            yield
        finally:
            if warmup is not None:
                warmup.cancel()
                await asyncio.gather(warmup, return_exceptions=True)
            await shutdown(queue=queue, worker=worker, supervisor=supervisor, session=session)

    server = MCPServer(name="speak", version=VERSION, lifespan=lifespan)

    async def session_voice() -> str | None:
        """Resolve the session voice, rolling one the first time it is needed.

        Resolution is lazy because a speech server may report no voices until it
        has warmed up. An unresolved voice means this utterance uses the engine
        default and the roll is retried on the next one.
        """
        if session.voice is not None or not session.randomize:
            return session.voice
        pool = choose_pool(settings.voice_pool, engine.random_pool, await engine.list_voices())
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
    ) -> str:
        try:
            spoken = normalize(text, max_chars=settings.max_chars)
        except ValueError as error:
            raise ToolError(str(error)) from error

        if supervisor is not None and not session.registered:
            session.registered = True
            await supervisor.register()

        ahead = queue.enqueue(
            spoken,
            voice=voice if voice is not None else await session_voice(),
            rate=rate if rate is not None else session.rate,
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

    if ears is not None:
        # The microphone lock is re-entrant by pid, so it separates sessions but not
        # two calls inside this one process; without this second gate a concurrent
        # pair would open the microphone together and release the lock out from under
        # each other. Refusing rather than queueing keeps the answer immediate.
        listening = asyncio.Lock()

        @server.tool(name="listen", description=tools.LISTEN)
        async def listen(
            max_seconds: Annotated[
                float | None, Field(description=tools.LISTEN_SECONDS, gt=0, allow_inf_nan=False)
            ] = None,
        ) -> str:
            if listening.locked():
                raise ToolError(tools.ALREADY_LISTENING)

            async with listening:
                try:
                    await asyncio.wait_for(queue.drain(), timeout=DRAIN_TIMEOUT)
                except TimeoutError:
                    # A queue that will not drain is a reason to record anyway, not a
                    # reason to refuse the user; the worst case is hearing ourselves.
                    logger.warning("speech queue did not drain; opening the microphone anyway")

                await settle(session.warmup)

                # Liveness first: `has_asr` answers False for any HTTP failure, so a
                # server that is not there at all would otherwise be reported as one
                # running without a transcription model, and neither remedy that
                # message offers would apply. A server that is up but still loading
                # its models gets its own answer for the same reason: "wait" is the
                # only correct advice, and neither other message gives it.
                state = await ears.transcriber.liveness()
                if state is Liveness.ABSENT:
                    raise ToolError(tools.SERVER_UNREACHABLE)
                if state is Liveness.STARTING:
                    raise ToolError(tools.SERVER_STARTING)
                if not await ears.transcriber.has_asr():
                    raise ToolError(tools.NO_ASR)

                try:
                    ears.mic_lock.acquire()
                except MicrophoneBusy as error:
                    raise ToolError(str(error)) from error

                try:
                    # The cues bracket the recording and nothing else: they say the
                    # microphone is open and that it has shut, which is the moment
                    # recording stops rather than the moment the transcript lands.
                    async with ears.cue.around_recording() as outcome:
                        recording = await ears.recorder.record(_limits(ears.limits, max_seconds))
                        outcome.heard = recording.has_speech
                    logger.info("capture: %s", describe(recording))
                    if not recording.has_speech:
                        return tools.HEARD_NOTHING
                    heard = await ears.transcriber.transcribe(
                        recording.wav, language=settings.listen_language
                    )
                except (TranscriptionFailed, RecorderUnavailable) as error:
                    raise ToolError(str(error)) from error
                finally:
                    ears.mic_lock.release()

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
