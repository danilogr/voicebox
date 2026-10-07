"""Narration rows are rows the server accepts, and stop_reading answers at once even
while another process holds the speakers."""

import asyncio
from collections.abc import Sequence
from typing import Never

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from speak_core.queue import SpeechQueue
from telemetry_support import INSTALL, SESSION, assert_shippable, opened

from fakes import FakeEngine, FakeSupervisor
from voicebox.config import Settings
from voicebox.narration import NarrationTracker
from voicebox.server import create_server
from voicebox.telemetry.session import TelemetrySession


class HeldElsewhere:
    """The speakers, held by another process for as long as this one is willing to wait."""

    def __init__(self) -> None:
        self.waiting = asyncio.Event()

    async def acquire(self, stopped: asyncio.Event) -> bool:
        self.waiting.set()
        await stopped.wait()
        return False

    def release(self) -> None:
        return None


async def test_stop_reading_while_waiting_for_the_speakers_is_immediate(tmp_path):
    """Review Focus 5."""
    db = tmp_path / "telemetry.db"
    telemetry = TelemetrySession(
        conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db
    )
    spawned: list[str] = []

    async def spawn(command: str, args: Sequence[str]) -> Never:
        spawned.append(command)
        raise AssertionError("nothing may play while another process holds the speakers")

    engine = FakeEngine()
    speaker = HeldElsewhere()
    narration: NarrationTracker

    def on_finish(outcome) -> None:
        narration.finished(outcome)

    queue = SpeechQueue(prepare=engine.prepare, spawn=spawn, on_finish=on_finish, speaker=speaker)
    narration = NarrationTracker(
        telemetry=telemetry,
        engine="say",
        model=None,
        voice=None,
        pending=lambda: queue.stats().size,
        idle_s=60,
    )
    server = create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=engine,
        queue=queue,
        supervisor=FakeSupervisor(),
        narration=narration,
    )
    worker = asyncio.create_task(queue.run())
    try:
        for sentence in ("One.", "Two.", "Three."):
            await server.call_tool("read_aloud", {"text": sentence})
        await asyncio.wait_for(speaker.waiting.wait(), 2)
        reply = await asyncio.wait_for(server.call_tool("stop_reading", {}), 1)
        assert reply.content[0].text.startswith("stopped; dropped")
        await asyncio.sleep(0.05)
        assert spawned == []
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    (row,) = assert_shippable(db)
    assert (row["kind"], row["status"], row["sentences"], row["sentences_played"]) == (
        "narration",
        "stopped",
        3,
        0,
    )


async def test_every_narration_outcome_is_a_shippable_row(tmp_path):
    from test_narration import outcome  # the same outcome builder the tracker tests use

    db = tmp_path / "telemetry.db"
    telemetry = TelemetrySession(
        conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db
    )

    def tracker(engine: str) -> NarrationTracker:
        return NarrationTracker(
            telemetry=telemetry,
            engine=engine,
            model="magpie" if engine == "nemo" else None,
            voice="My Voice",
            pending=lambda: 0,
            idle_s=60,
        )

    ok = tracker("nemo")
    ok.queued("One.")
    ok.finished(outcome())
    ok.close()
    stopped = tracker("say")
    stopped.queued("One.")
    stopped.queued("Two.")
    stopped.stopped()
    tracker("command").refused("input_unsupported")
    broken = tracker("nemo")
    broken.queued("One.")
    broken.finished(outcome("failed", play_ms=None, audio_ms=None))
    broken.close()
    # A run closed by the session ending with nothing played is `stopped`; only the idle
    # close reports `failed` (next test).
    rows = assert_shippable(db)
    assert sorted(row["status"] for row in rows) == ["ok", "refused", "stopped", "stopped"]


async def test_a_failed_narration_row_is_shippable(tmp_path):
    from test_narration import outcome

    db = tmp_path / "telemetry.db"
    telemetry = TelemetrySession(
        conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db
    )
    pending = [1]
    narration = NarrationTracker(
        telemetry=telemetry,
        engine="nemo",
        model="magpie",
        voice="John",
        pending=lambda: pending[0],
        idle_s=0.01,
    )
    narration.queued("One.")
    pending[0] = 0
    narration.finished(outcome("failed", play_ms=None, audio_ms=None))
    await asyncio.sleep(0.1)
    (row,) = assert_shippable(db)
    assert (row["status"], row["reason"], row["stage"]) == ("failed", "other", "play")


async def test_a_sentence_with_a_nul_is_one_refused_row_and_never_reaches_the_queue(tmp_path):
    db = tmp_path / "telemetry.db"
    telemetry = TelemetrySession(
        conn=opened(db), install_id=INSTALL, session_id=SESSION, db_path=db
    )
    narration = NarrationTracker(
        telemetry=telemetry, engine="say", model=None, voice=None, pending=lambda: 0, idle_s=60
    )
    engine = FakeEngine()
    queue = SpeechQueue(prepare=engine.prepare)
    server = create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=engine,
        queue=queue,
        supervisor=None,
        narration=narration,
    )
    with pytest.raises(ToolError, match="NUL"):
        await server.call_tool("read_aloud", {"text": "bad\x00text"})
    assert queue.stats().size == 0
    (row,) = assert_shippable(db)
    assert (row["kind"], row["status"], row["reason"]) == (
        "narration",
        "refused",
        "input_unsupported",
    )
