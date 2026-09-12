import asyncio

import pytest

from speak.engines.base import Utterance
from speak.queue import SpeechQueue


class FakeProcess:
    """A child process the test decides when to end."""

    def __init__(self) -> None:
        self.finished = asyncio.Event()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.finished.set()

    async def wait(self) -> int:
        await self.finished.wait()
        return 0


class Spawner:
    def __init__(self, *, autofinish: bool = True) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.processes: list[FakeProcess] = []
        self._autofinish = autofinish

    async def __call__(self, command: str, args: list[str]) -> FakeProcess:
        self.calls.append((command, list(args)))
        process = FakeProcess()
        if self._autofinish:
            process.finished.set()
        self.processes.append(process)
        return process


async def prepare_say(text: str, *, voice: str | None, rate: float | None) -> Utterance:
    return Utterance(command="say", args=[text])


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def running():
    """Build a queue with its worker task running, and tear it down."""
    started: list[tuple[SpeechQueue, asyncio.Task[None]]] = []

    def build(prepare=prepare_say, spawn=None, on_error=None) -> tuple[SpeechQueue, Spawner]:
        spawner = spawn or Spawner()
        queue = SpeechQueue(prepare=prepare, spawn=spawner, on_error=on_error)
        started.append((queue, asyncio.create_task(queue.run())))
        return queue, spawner

    yield build
    for _, task in started:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_enqueue_reports_how_many_utterances_are_ahead():
    queue = SpeechQueue(prepare=prepare_say, spawn=Spawner())
    assert queue.enqueue("first", voice=None, rate=None) == 0
    assert queue.enqueue("second", voice=None, rate=None) == 1
    assert queue.enqueue("third", voice=None, rate=None) == 2


async def test_plays_utterances_in_order(running):
    queue, spawner = running()
    for word in ("one", "two", "three"):
        queue.enqueue(word, voice=None, rate=None)
    await settle()
    assert spawner.calls == [("say", ["one"]), ("say", ["two"]), ("say", ["three"])]


async def test_runs_only_one_process_at_a_time(running):
    queue, spawner = running(spawn=Spawner(autofinish=False))
    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await settle()
    assert len(spawner.calls) == 1

    spawner.processes[0].finished.set()
    await settle()
    assert len(spawner.calls) == 2


async def test_stop_terminates_the_current_process_and_drops_the_rest(running):
    queue, spawner = running(spawn=Spawner(autofinish=False))
    for word in ("one", "two", "three"):
        queue.enqueue(word, voice=None, rate=None)
    await settle()

    assert await queue.stop() == 3
    assert spawner.processes[0].terminated is True
    await settle()
    assert len(spawner.calls) == 1


async def test_stop_on_an_idle_queue_drops_nothing(running):
    queue, _ = running()
    assert await queue.stop() == 0


async def test_a_failing_prepare_does_not_stall_the_utterances_behind_it(running):
    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        if text == "bad":
            raise RuntimeError("synthesis failed")
        return Utterance(command="say", args=[text])

    errors: list[BaseException] = []
    queue, spawner = running(prepare=prepare, on_error=errors.append)
    queue.enqueue("bad", voice=None, rate=None)
    queue.enqueue("good", voice=None, rate=None)
    await settle()

    assert spawner.calls == [("say", ["good"])]
    assert len(errors) == 1


async def test_a_failing_spawn_does_not_stall_the_queue(running):
    spawner = Spawner()
    calls: list[str] = []

    async def spawn(command: str, args: list[str]) -> FakeProcess:
        calls.append(args[0])
        if args[0] == "bad":
            raise OSError("no such binary")
        return await spawner(command, args)

    errors: list[BaseException] = []
    queue, _ = running(spawn=spawn, on_error=errors.append)
    queue.enqueue("bad", voice=None, rate=None)
    queue.enqueue("good", voice=None, rate=None)
    await settle()

    assert calls == ["bad", "good"]
    assert len(errors) == 1


async def test_cleanup_runs_exactly_once_per_utterance(running):
    cleaned: list[str] = []

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        async def cleanup() -> None:
            cleaned.append(text)

        return Utterance(command="afplay", args=[text], cleanup=cleanup)

    queue, _ = running(prepare=prepare)
    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await settle()
    assert cleaned == ["one", "two"]


async def test_cleanup_runs_when_an_utterance_is_stopped(running):
    cleaned: list[str] = []

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        async def cleanup() -> None:
            cleaned.append(text)

        return Utterance(command="afplay", args=[text], cleanup=cleanup)

    queue, _ = running(prepare=prepare, spawn=Spawner(autofinish=False))
    queue.enqueue("one", voice=None, rate=None)
    await settle()
    await queue.stop()
    await settle()
    assert cleaned == ["one"]


async def test_prepare_receives_the_voice_and_rate(running):
    seen: list[tuple[str | None, float | None]] = []

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        seen.append((voice, rate))
        return Utterance(command="say", args=[text])

    queue, _ = running(prepare=prepare)
    queue.enqueue("hi", voice="Sofia", rate=190)
    await settle()
    assert seen == [("Sofia", 190)]


async def test_stop_cancels_an_utterance_that_is_still_being_synthesized(running):
    """stop_speaking during a slow synthesis must not let the sentence out afterwards."""
    release = asyncio.Event()

    async def slow_prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        await release.wait()
        return Utterance(command="say", args=[text])

    queue, spawner = running(prepare=slow_prepare)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()

    assert await queue.stop() == 1
    release.set()
    await settle()

    assert spawner.calls == []


async def test_utterances_queued_after_a_stop_still_play(running):
    queue, spawner = running()
    queue.enqueue("dropped", voice=None, rate=None)
    await queue.stop()
    queue.enqueue("kept", voice=None, rate=None)
    await settle()
    assert spawner.calls == [("say", ["kept"])]


async def test_drain_returns_at_once_when_nothing_is_queued():
    queue = SpeechQueue(prepare=prepare_say, spawn=Spawner())
    await asyncio.wait_for(queue.drain(), timeout=1)


async def test_drain_waits_until_every_queued_utterance_has_played(running):
    queue, spawner = running(spawn=Spawner(autofinish=False))
    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)

    drained = asyncio.create_task(queue.drain())
    await settle()
    assert not drained.done()
    assert len(spawner.calls) == 1

    spawner.processes[0].finished.set()
    await settle()
    assert not drained.done()
    assert len(spawner.calls) == 2

    spawner.processes[1].finished.set()
    await asyncio.wait_for(drained, timeout=1)
    assert queue.size == 0


async def test_drain_waits_for_an_utterance_queued_behind_the_one_playing(running):
    queue, spawner = running(spawn=Spawner(autofinish=False))
    queue.enqueue("one", voice=None, rate=None)
    await settle()

    drained = asyncio.create_task(queue.drain())
    queue.enqueue("two", voice=None, rate=None)
    spawner.processes[0].finished.set()
    await settle()
    assert not drained.done()
    assert len(spawner.calls) == 2

    spawner.processes[1].finished.set()
    await asyncio.wait_for(drained, timeout=1)
    assert queue.size == 0


async def test_stop_releases_a_waiting_drain():
    queue = SpeechQueue(prepare=prepare_say, spawn=Spawner())
    queue.enqueue("hello", voice=None, rate=None)

    drained = asyncio.create_task(queue.drain())
    await settle()
    assert not drained.done()

    await queue.stop()
    await asyncio.wait_for(drained, timeout=1)


async def test_drain_waits_out_a_synthesis_that_stop_cancelled(running):
    """stop() during a slow prepare leaves work in flight; drain must wait for it."""
    release = asyncio.Event()

    async def slow_prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        await release.wait()
        return Utterance(command="say", args=[text])

    queue, spawner = running(prepare=slow_prepare)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()
    assert await queue.stop() == 1

    drained = asyncio.create_task(queue.drain())
    await settle()
    assert not drained.done()
    assert queue.size == 1

    release.set()
    await asyncio.wait_for(drained, timeout=1)
    assert queue.size == 0
    assert spawner.calls == []


async def test_stop_silences_an_utterance_caught_in_the_spawn_window(running):
    """The player is already starting when stop() lands; it must not be heard."""
    spawner = Spawner()
    release = asyncio.Event()

    async def slow_spawn(command: str, args: list[str]) -> FakeProcess:
        await release.wait()
        return await spawner(command, args)

    queue, _ = running(spawn=slow_spawn)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()

    assert await queue.stop() == 1
    release.set()
    await settle()

    assert spawner.processes[0].terminated is True
    assert queue.size == 0


async def test_drain_waits_out_an_utterance_stopped_while_it_was_spawning(running):
    spawner = Spawner(autofinish=False)
    release = asyncio.Event()

    async def slow_spawn(command: str, args: list[str]) -> FakeProcess:
        await release.wait()
        return await spawner(command, args)

    queue, _ = running(spawn=slow_spawn)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()

    drained = asyncio.create_task(queue.drain())
    await queue.stop()
    await settle()
    assert not drained.done()
    assert queue.size == 1

    release.set()
    await asyncio.wait_for(drained, timeout=1)
    assert queue.size == 0


async def test_drain_waits_as_long_as_the_queue_keeps_finishing():
    """One spoken answer is several utterances, each costing its synthesis as well as its
    audio, so a batch outlasts any fixed deadline. Every utterance that finishes resets
    the window, so only a stall ends the wait."""
    import asyncio as aio

    from speak.engines.base import Utterance
    from speak.queue import SpeechQueue

    class Quick:
        def terminate(self) -> None: ...

        async def wait(self) -> int:
            await aio.sleep(0.05)
            return 0

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        return Utterance(command="true", args=[])

    async def spawn(command: str, args: object) -> Quick:
        return Quick()

    queue = SpeechQueue(prepare=prepare, spawn=spawn)
    worker = aio.create_task(queue.run())
    for n in range(8):
        queue.enqueue(f"line {n}", voice=None, rate=None)

    # Eight at 50 ms each is 400 ms of work, far past the 120 ms window.
    await queue.drain(stall_timeout=0.12)

    assert queue.size == 0
    worker.cancel()
    await aio.gather(worker, return_exceptions=True)


async def test_drain_gives_up_when_nothing_finishes_at_all():
    """What a wedged worker looks like from here, and the only case that ends the wait."""
    import asyncio as aio

    import pytest as pt

    from speak.engines.base import Utterance
    from speak.queue import SpeechQueue

    class Wedged:
        def terminate(self) -> None: ...

        async def wait(self) -> int:
            await aio.Event().wait()
            return 0

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        return Utterance(command="true", args=[])

    async def spawn(command: str, args: object) -> Wedged:
        return Wedged()

    queue = SpeechQueue(prepare=prepare, spawn=spawn)
    worker = aio.create_task(queue.run())
    queue.enqueue("wedged", voice=None, rate=None)

    with pt.raises(TimeoutError):
        await queue.drain(stall_timeout=0.1)

    worker.cancel()
    await aio.gather(worker, return_exceptions=True)
