"""Narration rows are rows the server accepts, and stop_reading answers at once even
while another process holds the speakers."""

import asyncio
from collections.abc import Sequence
from typing import Never

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
    await ok.queued("One.")
    ok.finished(outcome())
    ok.close()
    stopped = tracker("say")
    await stopped.queued("One.")
    await stopped.queued("Two.")
    stopped.stopped()
    tracker("command").refused("input_unsupported")
    statuses = sorted(row["status"] for row in assert_shippable(db))
    assert statuses == ["ok", "refused", "stopped"]
