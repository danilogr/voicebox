"""Closing a session silences the speaker first."""

import asyncio

from fakes import FakeSupervisor
from voicebox.server import SessionState, shutdown


class RecordingQueue:
    """A queue that notes the order in which it is stopped and cancelled."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def enqueue(
        self, text: str, *, voice: str | None, rate: float | None, intent: str | None = None
    ) -> int:
        return 0

    async def stop(self) -> int:
        self.events.append("stopped")
        return 0

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("worker cancelled")
            raise


async def test_the_queue_is_silenced_before_the_worker_is_torn_down():
    events: list[str] = []
    queue = RecordingQueue(events)
    worker = asyncio.create_task(queue.run())
    await asyncio.sleep(0)

    await shutdown(queue=queue, worker=worker, supervisor=None, session=SessionState())

    assert events == ["stopped", "worker cancelled"]


async def test_the_supervisor_is_released_when_this_session_registered():
    supervisor = FakeSupervisor()
    queue = RecordingQueue([])
    worker = asyncio.create_task(queue.run())
    await asyncio.sleep(0)

    await shutdown(
        queue=queue,
        worker=worker,
        supervisor=supervisor,
        session=SessionState(registered=True),
    )
    assert supervisor.releases == 1


async def test_a_session_that_never_spoke_does_not_release_the_supervisor():
    supervisor = FakeSupervisor()
    queue = RecordingQueue([])
    worker = asyncio.create_task(queue.run())
    await asyncio.sleep(0)

    await shutdown(queue=queue, worker=worker, supervisor=supervisor, session=SessionState())
    assert supervisor.releases == 0


async def test_shutdown_waits_for_the_worker_to_finish():
    queue = RecordingQueue([])
    worker = asyncio.create_task(queue.run())
    await asyncio.sleep(0)

    await shutdown(queue=queue, worker=worker, supervisor=None, session=SessionState())
    assert worker.done()
