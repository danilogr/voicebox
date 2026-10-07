"""The MCP server: file transcription, background jobs, and a speech queue for reading
text aloud."""

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
from speak_core.telemetry.wiring import normalize_harness
from speak_core.text import normalize

from voicebox import tools
from voicebox.config import Settings
from voicebox.jobs.desk import Desk, busy_message
from voicebox.jobs.status import job_status as describe_jobs
from voicebox.telemetry.notice import Notice
from voicebox.telemetry.session import TelemetrySession
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
    # Set once the first tool call has looked for the MCP client's own `client_info`,
    # whether or not it found one: there is nothing more to learn by asking again.
    harness_checked: bool = False


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
    telemetry: TelemetrySession | None = None,
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


def create_server(
    *,
    settings: Settings,
    engine: Engine,
    queue: Queue,
    supervisor: Supervisor | None,
    desk: Desk | None = None,
    telemetry: TelemetrySession | None = None,
    notice: Notice | None = None,
    on_session_start: Callable[[], None] | None = None,
) -> MCPServer:
    session = SessionState()

    def reply(text: str) -> str:
        """Every successful tool reply goes through here, so the first one of a session
        that owes the first-run notice carries it."""
        shown = notice.take() if notice is not None else None
        return text if shown is None else f"{text}\n\n{shown}"

    def note_harness(ctx: Context) -> None:
        """Correct the session row's harness from the MCP client's own `client_info`, the
        first time a tool call can supply it. Only ever narrows: without it the row keeps
        what the environment decided at session start."""
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
            # Only here is a loop guaranteed to be running: `build()` calls this factory
            # before `server.run()` starts one.
            on_session_start()
        try:
            yield
        finally:
            await shutdown(
                queue=queue,
                worker=worker,
                supervisor=supervisor,
                session=session,
                telemetry=telemetry,
            )

    server = MCPServer(name="voicebox", version=VERSION, lifespan=lifespan)

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
            voice=voice if voice is not None else settings.speech.voice,
            rate=rate if rate is not None else settings.speech.rate,
            intent=intent,
        )
        if ahead == 0:
            return reply(f'speaking: "{spoken}"')
        return reply(f'queued behind {ahead} utterance{_plural(ahead)}: "{spoken}"')

    @server.tool(name="stop_speaking", description=tools.STOP_SPEAKING)
    async def stop_speaking(*, ctx: Context) -> str:
        note_harness(ctx)
        dropped = await queue.stop()
        return reply(f"stopped; dropped {dropped} utterance{_plural(dropped)}")

    @server.tool(name="speech_status", description=tools.SPEECH_STATUS)
    async def speech_status(*, ctx: Context) -> str:
        note_harness(ctx)
        stats = queue.stats()
        if stats.size == 0:
            return reply(f"nothing queued; {stats.finished} spoken so far")
        return reply(f"{describe_queue(stats)}; {stats.finished} spoken so far")

    if desk is not None:
        # The job lock is claimed for a worker that does not exist yet, so between a job
        # tool returning and that worker taking the lock over, the record still names
        # this process; without this second gate two calls in one turn would interleave
        # in that window. One gate for every job tool, because voicebox runs one job at a
        # time whatever its kind. Refusing rather than queueing keeps the answer immediate.
        starting = asyncio.Lock()

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
            if starting.locked():
                return reply(busy_message(desk))
            async with starting:
                return reply(
                    await transcription.start(
                        desk,
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
                )

        @server.tool(name="job_status", description=tools.JOB_STATUS)
        async def job_status(
            job_id: Annotated[str | None, Field(description=tools.JOB_ID)] = None,
            *,
            ctx: Context,
        ) -> str:
            note_harness(ctx)
            return reply(describe_jobs(desk, settings, job_id))

    return server
