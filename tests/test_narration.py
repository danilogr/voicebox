"""One narration row per read-aloud run: opened by the first sentence after the queue
was idle, closed by 30 s of nothing queued, by stop_reading, or by the session ending."""

import asyncio

from speak_core.queue import UtteranceOutcome

from voicebox.narration import NarrationTracker


class Recorded:
    """Stands in for a TelemetrySession: the ids, and the rows it was handed."""

    session_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    install_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.arrived = asyncio.Event()

    def record(self, row: dict[str, object]) -> None:
        self.rows.append(row)
        self.arrived.set()


def outcome(status: str = "ok", **overrides: object) -> UtteranceOutcome:
    fields = dict(
        text="A sentence.",
        voice="John",
        intent="narration",
        status=status,
        queue_wait_ms=5,
        synth_ms=400,
        play_ms=1200,
        audio_ms=1000,
        engine="nemo",
    )
    return UtteranceOutcome(**{**fields, **overrides})


def tracker(pending: list[int], *, engine: str = "nemo", warm=None, idle_s: float = 0.01):
    telemetry = Recorded()
    return telemetry, NarrationTracker(
        telemetry=telemetry,  # type: ignore[arg-type]
        engine=engine,
        model="magpie" if engine == "nemo" else None,
        voice="John",
        pending=lambda: pending[0],
        warm=warm,
        idle_s=idle_s,
    )


async def test_a_run_closes_ok_after_the_queue_stays_empty():
    pending = [1]
    telemetry, narration = tracker(pending)
    narration.queued("One sentence.")
    pending[0] = 0
    narration.finished(outcome())
    await asyncio.wait_for(telemetry.arrived.wait(), 1)
    (row,) = telemetry.rows
    assert (row["kind"], row["status"], row["engine"], row["model"]) == (
        "narration",
        "ok",
        "nemo",
        "magpie",
    )
    assert (row["sentences"], row["sentences_played"], row["chars"]) == (1, 1, 13)
    assert (row["audio_ms"], row["synth_ms"], row["voice"]) == (1000, 400, "John")


async def test_a_sentence_queued_inside_the_idle_window_continues_the_same_run():
    pending = [1]
    telemetry, narration = tracker(pending, idle_s=0.05)
    narration.queued("One.")
    pending[0] = 0
    narration.finished(outcome())
    narration.queued("Two.")
    await asyncio.sleep(0.1)
    assert telemetry.rows == []
    narration.finished(outcome())
    await asyncio.wait_for(telemetry.arrived.wait(), 1)
    (row,) = telemetry.rows
    assert (row["sentences"], row["sentences_played"]) == (2, 2)


async def test_stop_reading_before_the_end_is_stopped():
    pending = [3]
    telemetry, narration = tracker(pending)
    for text in ("One.", "Two.", "Three."):
        narration.queued(text)
    narration.finished(outcome())
    narration.stopped()
    (row,) = telemetry.rows
    assert (row["status"], row["sentences"], row["sentences_played"]) == ("stopped", 3, 1)
    assert "reason" not in row or row["reason"] is None


async def test_the_session_ending_closes_an_open_run():
    pending = [0]
    telemetry, narration = tracker(pending, idle_s=60)
    narration.queued("One.")
    narration.finished(outcome())
    narration.close()
    assert [r["status"] for r in telemetry.rows] == ["ok"]


async def test_nothing_played_and_something_failed_is_a_failed_run_at_play():
    pending = [1]
    telemetry, narration = tracker(pending)
    narration.queued("One.")
    pending[0] = 0
    narration.finished(outcome("failed", play_ms=None, audio_ms=None))
    await asyncio.wait_for(telemetry.arrived.wait(), 1)
    (row,) = telemetry.rows
    assert (row["status"], row["reason"], row["stage"]) == ("failed", "other", "play")


async def test_say_speaking_for_a_cold_nemo_is_counted_as_a_fallback():
    pending = [0]
    telemetry, narration = tracker(pending, idle_s=60)
    narration.queued("One.")
    narration.finished(outcome(engine="say", audio_ms=None, play_ms=900))
    narration.close()
    (row,) = telemetry.rows
    assert (row["fallback_sentences"], row["audio_ms"]) == (1, 900)


async def test_an_interrupted_sentence_counts_its_audio_but_not_as_played():
    pending = [0]
    telemetry, narration = tracker(pending, idle_s=60)
    narration.queued("One.")
    narration.finished(outcome("interrupted", play_ms=300))
    narration.close()
    (row,) = telemetry.rows
    assert (row["sentences_played"], row["audio_ms"], row["status"]) == (0, 300, "stopped")


async def test_server_warm_is_asked_once_per_run_and_only_when_given():
    asked = []

    async def warm() -> bool:
        asked.append(1)
        return True

    pending = [0]
    telemetry, narration = tracker(pending, warm=warm, idle_s=60)
    narration.queued("One.")
    narration.queued("Two.")
    await asyncio.sleep(0.01)
    narration.close()
    (row,) = telemetry.rows
    assert asked == [1] and row["server_warm"] is True and row["sentences"] == 2

    telemetry, narration = tracker([0], engine="say", idle_s=60)
    narration.queued("One.")
    narration.close()
    assert telemetry.rows[0]["server_warm"] is None


async def test_a_refused_sentence_is_a_refused_run_only_when_none_is_open():
    telemetry, narration = tracker([0], idle_s=60)
    narration.refused("input_unsupported")
    assert [(r["status"], r["reason"]) for r in telemetry.rows] == [
        ("refused", "input_unsupported")
    ]
    narration.queued("One.")
    narration.refused("input_unsupported")
    assert len(telemetry.rows) == 1


async def test_without_telemetry_nothing_is_recorded_and_nothing_breaks():
    narration = NarrationTracker(
        telemetry=None, engine="say", model=None, voice=None, pending=lambda: 0, idle_s=0.01
    )
    narration.queued("One.")
    narration.finished(outcome())
    narration.refused("input_unsupported")
    narration.stopped()
    narration.close()


async def test_a_slow_probe_never_delays_queued_and_a_run_closed_meanwhile_ignores_its_answer():
    release = asyncio.Event()

    async def warm() -> bool:
        await release.wait()
        return True

    telemetry, narration = tracker([0], warm=warm, idle_s=60)
    narration.queued("One.")  # returns at once, the probe still pending
    narration.stopped()
    release.set()
    await asyncio.sleep(0.01)
    (row,) = telemetry.rows
    assert row["status"] == "stopped" and row["server_warm"] is None


async def test_the_probe_of_a_stopped_run_does_not_mark_the_next_run():
    release = asyncio.Event()

    async def warm() -> bool:
        await release.wait()
        return True

    telemetry, narration = tracker([0], warm=warm, idle_s=60)
    narration.queued("One.")
    narration.stopped()
    narration.queued("Two.")
    narration.close()
    release.set()
    await asyncio.sleep(0.01)
    assert [r["server_warm"] for r in telemetry.rows] == [None, None]


async def test_outcomes_of_a_stopped_run_are_not_counted_in_the_next_run():
    pending = [0]
    telemetry, narration = tracker(pending, idle_s=60)
    narration.queued("One.")
    narration.queued("Two.")
    narration.stopped()
    narration.queued("Three.")
    narration.finished(outcome("interrupted", play_ms=300))  # One, from the stopped run
    narration.finished(outcome("discarded", play_ms=None, audio_ms=None, synth_ms=None))  # Two
    narration.finished(outcome())  # Three
    narration.close()
    stopped_run, second = telemetry.rows
    assert (stopped_run["status"], stopped_run["sentences"]) == ("stopped", 2)
    assert (second["status"], second["sentences"], second["sentences_played"]) == ("ok", 1, 1)
    assert second["audio_ms"] == 1000
