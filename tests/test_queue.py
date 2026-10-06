import asyncio
import time
from collections.abc import Callable

import pytest

from speak.engines.base import Utterance
from speak.queue import Process, SpeechQueue
from speak.speaker import WAITING_DIRNAME, SpeakerLock


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

    def build(
        prepare=prepare_say, spawn=None, on_error=None, on_finish=None, speaker=None
    ) -> tuple[SpeechQueue, Spawner]:
        spawner = spawn or Spawner()
        queue = SpeechQueue(
            prepare=prepare,
            spawn=spawner,
            on_error=on_error,
            on_finish=on_finish,
            speaker=speaker,
        )
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


class Controlled:
    """A prepare/spawn pair whose stages the test releases one at a time.

    Timing-free: each stage announces itself with an Event and waits for another, so a
    test can observe the queue mid-flight without sleeping.
    """

    def __init__(self) -> None:
        self.preparing = asyncio.Event()
        self.release_prepare = asyncio.Event()
        self.playing = asyncio.Event()
        self.release_play = asyncio.Event()
        self.prepared_texts: list[str] = []
        self.cleaned: list[str] = []

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance:
        self.prepared_texts.append(text)
        self.preparing.set()
        await self.release_prepare.wait()

        async def cleanup() -> None:
            self.cleaned.append(text)

        return Utterance(command="true", args=[text], cleanup=cleanup)

    async def spawn(self, command: str, args: object) -> "Controlled.Player":
        self.playing.set()
        return Controlled.Player(self.release_play)

    class Player:
        def __init__(self, release: asyncio.Event) -> None:
            self._release = release

        def terminate(self) -> None:
            self._release.set()

        async def wait(self) -> int:
            await self._release.wait()
            return 0


def test_an_idle_queue_reports_nothing_in_hand():
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    stats = queue.stats()
    assert (stats.waiting, stats.preparing, stats.prepared, stats.playing) == (0, 0, 0, False)
    assert stats.finished == 0


async def test_stats_count_an_utterance_through_every_stage():
    """`size` is derived from these, and `_idle` from `size`, so each stage has to be
    counted or `drain` releases while audio is still to come."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    await stage.preparing.wait()
    assert queue.stats().preparing == 1
    assert queue.size == 1

    stage.release_prepare.set()
    await stage.playing.wait()
    assert queue.stats().playing is True
    assert queue.size == 1

    stage.release_play.set()
    await queue.drain()
    stats = queue.stats()
    assert (stats.waiting, stats.preparing, stats.prepared, stats.playing) == (0, 0, 0, False)
    assert stats.finished == 1

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_waiting_utterances_are_counted_before_they_are_prepared():
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    for text in ("one", "two", "three"):
        queue.enqueue(text, voice=None, rate=None)
    await stage.preparing.wait()

    assert queue.stats().waiting == 2
    assert queue.size == 3

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_the_next_utterance_is_prepared_while_one_plays():
    """Half of a spoken answer's wall clock is synthesis; overlapping it with playback is
    the whole point of the queue having a worker at all."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)

    await stage.preparing.wait()
    stage.release_prepare.set()
    await stage.playing.wait()

    # "one" is playing; "two" must already have been asked for.
    await asyncio.sleep(0)
    assert stage.prepared_texts == ["one", "two"]

    stage.release_play.set()
    await queue.drain()
    assert queue.stats().finished == 2

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_order_is_preserved_with_a_prefetch_in_hand():
    """FIFO order holds even while a prefetch is genuinely in hand, not just prepared
    ahead of a play that had already finished by the time it ran."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    for text in ("one", "two", "three", "four"):
        queue.enqueue(text, voice=None, rate=None)

    await stage.preparing.wait()
    stage.release_prepare.set()
    await stage.playing.wait()
    await asyncio.sleep(0)

    # "one" is playing and "two" is already prepared and waiting: a real overlap.
    assert queue.stats().prepared == 1
    assert queue.stats().playing is True

    stage.release_play.set()
    await queue.drain()

    assert stage.prepared_texts == ["one", "two", "three", "four"]

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_drain_does_not_release_while_a_prefetched_utterance_waits():
    """`drain` must not report idle while audio is still to come: the microphone would
    then open over our own voice and hand it back as the user's answer."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await stage.preparing.wait()
    stage.release_prepare.set()
    await stage.playing.wait()
    await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        await queue.drain(stall_timeout=0.05)
    assert queue.size >= 2

    stage.release_play.set()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_stop_discards_a_prepared_utterance_and_deletes_its_file():
    """A prefetched utterance holds a temp WAV. Dropping it without cleanup leaks a file
    per stopped sentence."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await stage.preparing.wait()
    stage.release_prepare.set()
    await stage.playing.wait()
    await asyncio.sleep(0)

    await queue.stop()
    await queue.drain()

    assert "two" in stage.cleaned
    assert queue.size == 0

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_a_failure_preparing_the_next_one_does_not_disturb_this_one():
    """A prefetch that fails while the previous utterance is still audibly playing must
    not touch that playback, or the ones still queued behind it."""
    errors: list[BaseException] = []
    texts: list[str] = []
    playing = asyncio.Event()
    release_play = asyncio.Event()

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        texts.append(text)
        if text == "two":
            raise RuntimeError("synthesis failed")
        return Utterance(command="true", args=[text])

    class Player:
        def terminate(self) -> None:
            release_play.set()

        async def wait(self) -> int:
            await release_play.wait()
            return 0

    async def spawn(command: str, args: object) -> Player:
        playing.set()
        return Player()

    queue = SpeechQueue(prepare=prepare, spawn=spawn, on_error=errors.append)
    worker = asyncio.create_task(queue.run())
    for text in ("one", "two", "three"):
        queue.enqueue(text, voice=None, rate=None)

    await playing.wait()
    await asyncio.sleep(0)

    # "one" is playing; the prefetch for "two" has already failed without disturbing it.
    assert queue.stats().playing is True
    assert texts == ["one", "two"]
    assert len(errors) == 1

    release_play.set()
    await queue.drain()

    assert texts == ["one", "two", "three"]
    assert len(errors) == 1
    assert queue.stats().finished == 3
    assert queue.size == 0

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_a_discarded_prefetch_is_counted_until_its_file_is_gone():
    """`_idle` is derived from `size`. A discard that stops counting before its cleanup
    has finished lets a second `stop` set `_idle` with the temp file still on disk."""
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    playing = asyncio.Event()
    release_play = asyncio.Event()

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        async def cleanup() -> None:
            if text == "two":
                cleaning.set()
                await release_cleanup.wait()

        return Utterance(command="true", args=[text], cleanup=cleanup)

    class Player:
        def terminate(self) -> None:
            release_play.set()

        async def wait(self) -> int:
            await release_play.wait()
            return 0

    async def spawn(command: str, args: object) -> Player:
        playing.set()
        return Player()

    queue = SpeechQueue(prepare=prepare, spawn=spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await playing.wait()
    await asyncio.sleep(0)

    await queue.stop()
    await cleaning.wait()
    assert queue.size == 1

    release_cleanup.set()
    await queue.drain()
    assert queue.size == 0

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_stop_discards_an_utterance_whose_prepare_task_has_not_started():
    """`stop()` landing between a prefetch task's creation and its first step must still
    discard that utterance. Reading the generation inside the task body, rather than at
    creation, would let a stop that races the task's first step capture the wrong value
    and let the utterance play anyway."""
    release_prepare = asyncio.Event()
    spawn_calls: list[str] = []
    cleaned: list[str] = []

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        await release_prepare.wait()

        async def cleanup() -> None:
            cleaned.append(text)

        return Utterance(command="true", args=[text], cleanup=cleanup)

    class Instant:
        def terminate(self) -> None: ...

        async def wait(self) -> int:
            return 0

    async def spawn(command: str, args: object) -> Instant:
        spawn_calls.append(command)
        return Instant()

    queue = SpeechQueue(prepare=prepare, spawn=spawn)
    worker = asyncio.create_task(queue.run())
    await asyncio.sleep(0)  # let the worker park on `_wake`

    go = asyncio.Event()

    async def stopper() -> None:
        await go.wait()
        await queue.stop()

    stop_task = asyncio.create_task(stopper())
    await asyncio.sleep(0)  # let the stopper park on `go`

    # One synchronous step: enqueue, then release the stopper. `_wake` was set first, so
    # the worker runs before the stopper: it pops "one" and creates the prepare task,
    # whose first step is scheduled behind the stopper's. `stop()` then runs before that
    # task has read anything at all.
    queue.enqueue("one", voice=None, rate=None)
    go.set()

    await stop_task
    release_prepare.set()
    await queue.drain()

    assert spawn_calls == []
    assert cleaned == ["one"]
    assert queue.size == 0

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_cancelling_the_worker_discards_a_prefetch_in_hand():
    """Tearing down the worker while a prefetch is in hand must not orphan it: the temp
    file it is holding is still cleaned up and it stops being counted, the same as any
    other utterance `stop()` discards."""
    stage = Controlled()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)

    await stage.preparing.wait()
    stage.release_prepare.set()
    await stage.playing.wait()
    await asyncio.sleep(0)

    # "one" is playing; "two" is prepared and waiting to play next.
    assert queue.stats().prepared == 1

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert "two" in stage.cleaned
    assert queue.size == 0


async def test_a_prefetch_cancelled_before_it_starts_is_not_left_in_the_count():
    """A task cancelled before its first step never runs its body, so a `finally` there
    cannot release it. `size` reads the prefetch reference itself for that reason."""
    started = asyncio.Event()

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        started.set()
        return Utterance(command="true", args=[text])

    queue = SpeechQueue(prepare=prepare, spawn=Spawner())
    worker = asyncio.create_task(queue.run())
    await settle()

    cancel_now = asyncio.Event()

    async def cancel_worker() -> None:
        await cancel_now.wait()
        worker.cancel()

    canceller = asyncio.create_task(cancel_worker())
    await settle()

    # One synchronous step, so the canceller is queued ahead of the prepare task the
    # worker is about to create: the worker pops, creates it, and suspends before it runs.
    queue.enqueue("one", voice=None, rate=None)
    cancel_now.set()
    await asyncio.gather(worker, canceller, return_exceptions=True)

    assert not started.is_set()
    assert queue.stats().preparing == 0
    assert queue.size == 0


async def test_a_worker_cancelled_while_the_player_starts_still_deletes_the_file():
    """Teardown can land in the window between asking for a player and getting one.
    Cleanup belongs to `_play`, not to the spawn, so that window is covered too."""
    cleaned: list[str] = []
    spawning = asyncio.Event()

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        async def cleanup() -> None:
            cleaned.append(text)

        return Utterance(command="true", args=[text], cleanup=cleanup)

    async def slow_spawn(command: str, args: object) -> Process:
        spawning.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    queue = SpeechQueue(prepare=prepare, spawn=slow_spawn)
    worker = asyncio.create_task(queue.run())
    queue.enqueue("one", voice=None, rate=None)
    await spawning.wait()

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert cleaned == ["one"]
    assert queue.size == 0


async def test_a_worker_cancelled_mid_cleanup_does_not_strand_the_utterance():
    """Deleting the temp file is an `await` in production, so teardown can land inside
    it. The file may then survive, but the utterance still has to leave the count: a
    queue stuck at `size` 1 refuses every later `listen` for the life of the process."""
    cleaning = asyncio.Event()

    async def prepare(text: str, *, voice: str | None, rate: float | None) -> Utterance:
        async def cleanup() -> None:
            cleaning.set()
            await asyncio.Event().wait()

        return Utterance(command="true", args=[text], cleanup=cleanup)

    queue = SpeechQueue(prepare=prepare, spawn=Spawner())
    worker = asyncio.create_task(queue.run())
    queue.enqueue("one", voice=None, rate=None)
    await cleaning.wait()

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert queue.size == 0
    assert queue.stats().finished == 1


async def test_size_never_dips_while_an_utterance_changes_hands():
    """The worker hands an utterance straight from `_ahead` to `_inflight`. A yield
    opening between the two would read as an empty queue with audio still to come."""
    stage = Controlled()
    stage.release_prepare.set()
    stage.release_play.set()
    queue = SpeechQueue(prepare=stage.prepare, spawn=stage.spawn)
    worker = asyncio.create_task(queue.run())

    sizes: list[int] = []

    async def watch() -> None:
        while queue.stats().finished == 0:
            sizes.append(queue.size)
            await asyncio.sleep(0)

    queue.enqueue("one", voice=None, rate=None)
    watcher = asyncio.create_task(watch())
    await queue.drain()
    await watcher

    assert sizes
    assert 0 not in sizes

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


# --- telemetry: on_finish reports one outcome per utterance that leaves the queue ---


async def test_on_finish_reports_ok_with_timings_for_a_normal_play(running):
    from speak.queue import UtteranceOutcome

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(on_finish=outcomes.append)
    queue.enqueue("hello there", voice="Sofia", rate=None, intent="task_done")
    await settle()

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.text == "hello there"
    assert outcome.voice == "Sofia"
    assert outcome.intent == "task_done"
    assert outcome.status == "ok"
    assert outcome.queue_wait_ms >= 0
    assert outcome.synth_ms is not None
    assert outcome.play_ms is not None


async def test_on_finish_carries_the_utterances_own_audio_ms(running):
    from speak.queue import UtteranceOutcome

    async def prepare(text, *, voice, rate) -> Utterance:
        return Utterance(command="true", args=[text], audio_ms=1234)

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(prepare=prepare, on_finish=outcomes.append)
    queue.enqueue("hi", voice=None, rate=None)
    await settle()

    assert outcomes[0].audio_ms == 1234


async def test_on_finish_reports_failed_when_prepare_raises(running):
    from speak.queue import UtteranceOutcome

    async def prepare(text, *, voice, rate) -> Utterance:
        raise RuntimeError("synthesis failed")

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(prepare=prepare, on_error=lambda e: None, on_finish=outcomes.append)
    queue.enqueue("bad", voice=None, rate=None)
    await settle()

    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].play_ms is None


async def test_on_finish_reports_failed_when_the_spawn_itself_fails(running):
    from speak.queue import UtteranceOutcome

    async def spawn(command: str, args: list[str]) -> FakeProcess:
        raise OSError("no such binary")

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(spawn=spawn, on_error=lambda e: None, on_finish=outcomes.append)
    queue.enqueue("hi", voice=None, rate=None)
    await settle()

    assert outcomes[0].status == "failed"


async def test_on_finish_reports_discarded_for_an_utterance_dropped_while_waiting(running):
    from speak.queue import UtteranceOutcome

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(spawn=Spawner(autofinish=False), on_finish=outcomes.append)
    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    queue.enqueue("three", voice=None, rate=None)
    await settle()

    await queue.stop()
    await settle()

    discarded = [o for o in outcomes if o.status == "discarded"]
    assert {o.text for o in discarded} == {"two", "three"}


async def test_on_finish_reports_discarded_for_an_utterance_stopped_mid_prepare(running):
    from speak.queue import UtteranceOutcome

    release = asyncio.Event()

    async def slow_prepare(text, *, voice, rate) -> Utterance:
        await release.wait()
        return Utterance(command="say", args=[text])

    outcomes: list[UtteranceOutcome] = []
    queue, spawner = running(prepare=slow_prepare, on_finish=outcomes.append)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()

    assert await queue.stop() == 1
    release.set()
    await settle()

    assert spawner.calls == []
    assert len(outcomes) == 1
    assert outcomes[0].status == "discarded"
    assert outcomes[0].play_ms is None


async def test_on_finish_reports_discarded_for_an_utterance_stopped_during_spawn(running):
    from speak.queue import UtteranceOutcome

    spawner = Spawner()
    release = asyncio.Event()

    async def slow_spawn(command: str, args: list[str]) -> FakeProcess:
        await release.wait()
        return await spawner(command, args)

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(spawn=slow_spawn, on_finish=outcomes.append)
    queue.enqueue("cancel me", voice=None, rate=None)
    await settle()

    assert await queue.stop() == 1
    release.set()
    await settle()

    assert len(outcomes) == 1
    assert outcomes[0].status == "discarded"
    assert outcomes[0].play_ms is not None  # the process did briefly run


async def test_a_telemetry_callback_that_raises_does_not_break_playback(running):
    from speak.queue import UtteranceOutcome

    def exploding_callback(outcome: UtteranceOutcome) -> None:
        raise RuntimeError("telemetry backend is down")

    queue, spawner = running(on_finish=exploding_callback)
    queue.enqueue("one", voice=None, rate=None)
    queue.enqueue("two", voice=None, rate=None)
    await settle()

    assert spawner.calls == [("say", ["one"]), ("say", ["two"])]


class ExitingProcess(FakeProcess):
    """A player whose exit status the test chooses, and which reports the signal
    exit status a real player gives when it is terminated."""

    def __init__(self, returncode: int = 0) -> None:
        super().__init__()
        self._returncode = returncode

    async def wait(self) -> int:
        await self.finished.wait()
        return -15 if self.terminated else self._returncode


async def test_a_stop_during_playback_reports_interrupted_not_ok(running):
    """Cut off while audible is its own outcome: counting it as `ok` hides every
    interruption, and counting it as `discarded` merges it with sentences nobody
    heard at all."""
    from speak.queue import UtteranceOutcome

    process = ExitingProcess()

    async def spawn(command: str, args: list[str]) -> ExitingProcess:
        return process

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(spawn=spawn, on_finish=outcomes.append)
    queue.enqueue("cut me off", voice=None, rate=None)
    await settle()
    assert queue.stats().playing

    await queue.stop()
    await settle()

    assert process.terminated
    assert [o.status for o in outcomes] == ["interrupted"]
    assert outcomes[0].play_ms is not None


async def test_a_player_that_exits_non_zero_on_its_own_reports_failed(running):
    from speak.queue import UtteranceOutcome

    async def spawn(command: str, args: list[str]) -> ExitingProcess:
        process = ExitingProcess(returncode=1)
        process.finished.set()
        return process

    errors: list[BaseException] = []
    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(spawn=spawn, on_error=errors.append, on_finish=outcomes.append)
    queue.enqueue("broken player", voice=None, rate=None)
    await settle()

    assert [o.status for o in outcomes] == ["failed"]
    assert len(errors) == 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


async def test_queue_wait_ends_when_synthesis_starts_not_when_playback_ends(running, monkeypatch):
    """`queue_wait_ms` is time spent behind other utterances. The first sentence
    waits for nothing however long it plays; the second waits only for the first
    one's synthesis, because it is prepared while the first is playing."""
    from speak.queue import UtteranceOutcome

    clock = FakeClock()
    monkeypatch.setattr("speak.queue.time", clock)

    async def prepare(text, *, voice, rate) -> Utterance:
        clock.now += 1.0  # each synthesis takes one second
        return Utterance(command="say", args=[text])

    outcomes: list[UtteranceOutcome] = []
    queue, spawner = running(
        prepare=prepare, spawn=Spawner(autofinish=False), on_finish=outcomes.append
    )
    queue.enqueue("first", voice=None, rate=None)
    queue.enqueue("second", voice=None, rate=None)
    await settle()

    clock.now = 30.0  # the first sentence plays for a long time
    spawner.processes[0].finished.set()
    await settle()
    spawner.processes[1].finished.set()
    await settle()

    waits = {o.text: o.queue_wait_ms for o in outcomes}
    assert waits == {"first": 0, "second": 1000}
    assert all(o.synth_ms == 1000 for o in outcomes)


async def test_on_finish_passes_through_the_engine_and_voice_the_utterance_reported(running):
    from speak.queue import UtteranceOutcome

    async def prepare(text, *, voice, rate) -> Utterance:
        return Utterance(command="say", args=[text], engine="say", voice=None)

    outcomes: list[UtteranceOutcome] = []
    queue, _ = running(prepare=prepare, on_finish=outcomes.append)
    queue.enqueue("hi", voice="Sofia", rate=None)
    await settle()

    assert (outcomes[0].engine, outcomes[0].voice) == ("say", None)


def speaker(directory, pid, *, poll=0.01):
    return SpeakerLock(shared_dir=directory, pid=pid, is_alive=lambda p: p in (1, 2), poll=poll)


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


async def test_two_queues_sharing_the_speaker_never_play_at_once(running, tmp_path):
    first, first_spawner = running(spawn=Spawner(autofinish=False), speaker=speaker(tmp_path, 1))
    second, second_spawner = running(spawn=Spawner(autofinish=False), speaker=speaker(tmp_path, 2))

    first.enqueue("one", voice=None, rate=None)
    await until(lambda: len(first_spawner.calls) == 1)
    second.enqueue("two", voice=None, rate=None)
    await until(lambda: (tmp_path / WAITING_DIRNAME / "2").exists())
    assert second_spawner.calls == []

    first_spawner.processes[0].finished.set()
    await until(lambda: len(second_spawner.calls) == 1)
    second_spawner.processes[0].finished.set()
    await asyncio.wait_for(second.drain(), timeout=2.0)


async def test_a_queue_waiting_for_the_speaker_stops_at_once(running, tmp_path):
    outcomes = []
    first, first_spawner = running(spawn=Spawner(autofinish=False), speaker=speaker(tmp_path, 1))
    second, second_spawner = running(
        spawn=Spawner(autofinish=False),
        speaker=speaker(tmp_path, 2, poll=60.0),
        on_finish=outcomes.append,
    )
    first.enqueue("one", voice=None, rate=None)
    await until(lambda: len(first_spawner.calls) == 1)
    second.enqueue("two", voice=None, rate=None)
    await until(lambda: (tmp_path / WAITING_DIRNAME / "2").exists())

    assert await second.stop() == 1
    await asyncio.wait_for(second.drain(), timeout=1.0)

    assert [outcome.status for outcome in outcomes] == ["discarded"]
    assert second_spawner.calls == []
    first_spawner.processes[0].finished.set()


async def test_the_speaker_is_released_when_playback_is_cancelled(tmp_path):
    spawner = Spawner(autofinish=False)
    queue = SpeechQueue(prepare=prepare_say, spawn=spawner, speaker=speaker(tmp_path, 1))
    worker = asyncio.create_task(queue.run())
    queue.enqueue("one", voice=None, rate=None)
    await until(lambda: len(spawner.calls) == 1)

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert not (tmp_path / "speaker").exists()
    other = speaker(tmp_path, 2)
    assert await asyncio.wait_for(other.acquire(asyncio.Event()), 1.0) is True
