"""The MCP server: file transcription, and a speech queue for reading text aloud."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from speak_core.engines.base import Engine
from speak_core.queue import QueueStats
from speak_core.queue import describe as describe_queue
from speak_core.text import normalize

from voicebox import tools
from voicebox.config import Settings
from voicebox.transcribe import service as transcription

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


class Queue(Protocol):
    def enqueue(
        self, text: str, *, voice: str | None, rate: float | None, intent: str | None = None
    ) -> int: ...

    async def stop(self) -> int: ...

    async def run(self) -> None: ...

    def stats(self) -> QueueStats: ...


class Supervisor(Protocol):
    async def register(self) -> None: ...

    async def release(self) -> bool: ...

    async def ensure_running(self) -> bool: ...


@dataclass(slots=True)
class SessionState:
    """What this connection has done that its shutdown must undo. Never written to disk."""

    registered: bool = False


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def client_info(ctx: Context) -> tuple[str, str | None] | None:
    """(name, version) from the MCP client's own `client_info`, or `None`.

    `None` covers two real cases alike: `ctx.session.client_params` is `None` when a
    host declares no client info at all, and `ctx.session` itself raises outside of a
    live request - which every test that calls a tool directly does. Either way the
    caller keeps what the environment already decided.
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
    session: SessionState,
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


def create_server(
    *,
    settings: Settings,
    engine: Engine,
    queue: Queue,
    supervisor: Supervisor | None,
    scribe: "transcription.Scribe | None" = None,
    on_session_start: Callable[[], None] | None = None,
) -> MCPServer:
    session = SessionState()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        worker = asyncio.create_task(queue.run())
        if on_session_start is not None:
            # Only here is a loop guaranteed to be running: `build()` calls this factory
            # before `server.run()` starts one.
            on_session_start()
        try:
            yield
        finally:
            await shutdown(queue=queue, worker=worker, supervisor=supervisor, session=session)

    server = MCPServer(name="voicebox", version=VERSION, lifespan=lifespan)

    @server.tool(name="speak", description=tools.SPEAK)
    async def speak(
        text: Annotated[str, Field(description=tools.TEXT)],
        voice: Annotated[str | None, Field(description=tools.VOICE_OVERRIDE)] = None,
        rate: Annotated[float | None, Field(description=tools.RATE)] = None,
        intent: Annotated[tools.Intent | None, Field(description=tools.INTENT)] = None,
    ) -> str:
        try:
            spoken = normalize(text, max_chars=settings.speech.max_chars)
        except ValueError as error:
            raise ToolError(str(error)) from error
        if supervisor is not None and not session.registered:
            session.registered = True
            await supervisor.register()
        ahead = queue.enqueue(
            spoken,
            voice=voice if voice is not None else settings.speech.voice,
            rate=rate if rate is not None else settings.speech.rate,
            intent=intent,
        )
        if ahead == 0:
            return f'speaking: "{spoken}"'
        return f'queued behind {ahead} utterance{_plural(ahead)}: "{spoken}"'

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
        # The job lock is claimed for a worker that does not exist yet, so between this
        # call returning and that worker taking the lock over, the record still names
        # this process; without this second gate a concurrent pair would interleave in
        # that window. Refusing rather than queueing keeps the answer immediate.
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
        ) -> str:
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
                )

        @server.tool(name="transcription_status", description=tools.TRANSCRIPTION_STATUS)
        async def transcription_status(
            job_id: Annotated[str | None, Field(description=tools.TRANSCRIPTION_JOB_ID)] = None,
        ) -> str:
            return transcription.status(scribe, settings, job_id)

    return server
