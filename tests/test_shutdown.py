"""Opening a session warms the server; closing one silences the speaker first."""

import asyncio

from fakes import FakeMicLock, FakeRecorder, FakeSupervisor, FakeTranscriber
from speak import server as server_module
from speak.capture import Recording, State, TrackerSettings
from speak.cue import Cue
from speak.server import Ears, SessionState, settle, shutdown, start_listening


class RecordingQueue:
    """A queue that notes the order in which it is stopped and cancelled."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def enqueue(self, text: str, *, voice: str | None, rate: float | None) -> int:
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


def ears(*, reachable: bool = False) -> Ears:
    """Ears for a session that can listen; `reachable` says whether a server is up."""
    return Ears(
        recorder=FakeRecorder(Recording(wav=b"", reason=State.DONE, has_speech=True)),
        transcriber=FakeTranscriber(reachable=reachable),
        mic_lock=FakeMicLock(),
        limits=TrackerSettings(),
        cue=Cue(enabled=False),
    )


async def test_listening_registers_and_warms_the_speech_server():
    supervisor = FakeSupervisor()
    session = SessionState()

    warmup = await start_listening(supervisor=supervisor, session=session, ears=ears())

    assert warmup is not None
    assert await warmup is True
    assert supervisor.registrations == 1
    assert supervisor.starts == 1
    assert session.registered is True


async def test_a_session_that_never_listens_does_not_warm_the_speech_server():
    supervisor = FakeSupervisor()
    session = SessionState()

    assert await start_listening(supervisor=supervisor, session=session, ears=None) is None
    assert supervisor.registrations == 0
    assert supervisor.starts == 0
    assert session.registered is False


async def test_listening_without_a_supervisor_starts_nothing():
    session = SessionState()
    assert await start_listening(supervisor=None, session=session, ears=ears()) is None
    assert session.registered is False


async def test_a_speech_server_that_is_already_up_is_not_started_again():
    """Two listening sessions must share one server, not orphan the first one."""
    supervisor = FakeSupervisor()
    session = SessionState()

    warmup = await start_listening(
        supervisor=supervisor, session=session, ears=ears(reachable=True)
    )

    assert warmup is not None
    assert await warmup is False
    assert supervisor.starts == 0
    assert supervisor.registrations == 1


async def test_the_warmup_is_recorded_on_the_session():
    """`listen` has to be able to wait for it; nothing else in the session can."""
    session = SessionState()

    warmup = await start_listening(supervisor=FakeSupervisor(), session=session, ears=ears())

    assert session.warmup is warmup
    assert await warmup is True


async def test_settling_waits_for_a_warmup_that_is_still_running():
    gate = asyncio.Event()
    supervisor = FakeSupervisor(blocked_by=gate)
    session = SessionState()
    warmup = await start_listening(supervisor=supervisor, session=session, ears=ears())
    assert warmup is not None

    settling = asyncio.create_task(settle(warmup))
    await asyncio.sleep(0)
    assert not settling.done()

    gate.set()
    await asyncio.wait_for(settling, timeout=5.0)


async def test_settling_leaves_the_warmup_running_when_it_gives_up(monkeypatch):
    """`wait_for` would cancel the very thing that is about to make `listen` work."""
    monkeypatch.setattr(server_module, "WARMUP_TIMEOUT", 0.01)
    gate = asyncio.Event()
    supervisor = FakeSupervisor(blocked_by=gate)
    session = SessionState()
    warmup = await start_listening(supervisor=supervisor, session=session, ears=ears())
    assert warmup is not None

    await asyncio.wait_for(settle(warmup), timeout=5.0)

    assert not warmup.cancelled()
    gate.set()
    assert await warmup is True


async def test_settling_without_a_warmup_returns_at_once():
    await asyncio.wait_for(settle(None), timeout=5.0)


async def test_the_warmup_does_not_delay_the_session_opening():
    gate = asyncio.Event()
    supervisor = FakeSupervisor(blocked_by=gate)

    # Awaiting the warmup here would hold the session shut for the whole cold load.
    warmup = await asyncio.wait_for(
        start_listening(supervisor=supervisor, session=SessionState(), ears=ears()),
        timeout=5.0,
    )

    assert warmup is not None
    assert not warmup.done()
    gate.set()
    assert await warmup is True
