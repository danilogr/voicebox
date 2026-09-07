from dataclasses import replace

from speak.capture import FINAL, SilenceTracker, State, TrackerSettings

LIMITS = TrackerSettings(
    block_ms=100.0,
    calibrate_ms=300.0,
    silence_ms=500.0,
    start_timeout_ms=2000.0,
    max_ms=5000.0,
    noise_multiple=3.0,
    floor=0.01,
)

# Under LIMITS the start timeout always fires before the cap, so the cap is only
# ever reachable from SPEAKING. These limits wait long enough for a speaker that
# the cap arrives first, which is the only way to see it bound a silent phase.
PATIENT = replace(LIMITS, start_timeout_ms=9000.0)


def run(energies: list[float], limits: TrackerSettings = LIMITS) -> tuple[State, int]:
    """Feed blocks until a final state, reporting it and the block it landed on."""
    tracker = SilenceTracker(limits)
    state = tracker.state
    blocks = 0
    for energy in energies:
        blocks += 1
        state = tracker.feed(energy)
        if state in FINAL:
            break
    return state, blocks


def test_a_quiet_room_calibrates_to_the_floor():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.0)
    assert tracker.threshold == LIMITS.floor
    assert tracker.state is State.WAITING


def test_a_noisy_room_calibrates_above_the_floor():
    tracker = SilenceTracker(LIMITS)
    for _ in range(3):
        tracker.feed(0.02)
    assert tracker.threshold == 0.06
    assert tracker.state is State.WAITING


def test_silence_throughout_gives_up_once_the_start_timeout_passes():
    assert run([0.0] * 100) == (State.SILENT, 20)


def test_speech_followed_by_silence_finishes():
    assert run([0.0] * 3 + [0.5] * 5 + [0.0] * 5) == (State.DONE, 13)


def test_a_short_pause_does_not_end_the_utterance():
    tracker = SilenceTracker(LIMITS)
    for energy in [0.0] * 3 + [0.5] * 3 + [0.0] * 4 + [0.5] + [0.0] * 4 + [0.5]:
        state = tracker.feed(energy)
    assert state is State.SPEAKING


def test_speech_that_never_stops_hits_the_cap():
    assert run([0.0] * 3 + [0.5] * 100) == (State.CAPPED, 50)


def test_the_cap_also_bounds_a_recording_that_never_starts():
    assert run([0.0] * 100, PATIENT) == (State.CAPPED, 50)


def test_a_final_state_is_sticky():
    tracker = SilenceTracker(LIMITS)
    for _ in range(20):
        tracker.feed(0.0)
    assert tracker.state is State.SILENT
    for _ in range(5):
        assert tracker.feed(0.0) is State.SILENT
