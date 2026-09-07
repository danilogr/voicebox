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


def run(energies: list[float], limits: TrackerSettings = LIMITS) -> State:
    tracker = SilenceTracker(limits)
    state = tracker.state
    for energy in energies:
        state = tracker.feed(energy)
        if state in FINAL:
            break
    return state


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
    assert run([0.0] * 100) is State.SILENT


def test_speech_followed_by_silence_finishes():
    assert run([0.0] * 3 + [0.5] * 5 + [0.0] * 5) is State.DONE


def test_a_short_pause_does_not_end_the_utterance():
    tracker = SilenceTracker(LIMITS)
    for energy in [0.0] * 3 + [0.5] * 3 + [0.0] * 4 + [0.5]:
        state = tracker.feed(energy)
    assert state is State.SPEAKING


def test_speech_that_never_stops_hits_the_cap():
    assert run([0.0] * 3 + [0.5] * 100) is State.CAPPED


def test_a_final_state_is_sticky():
    tracker = SilenceTracker(LIMITS)
    for energy in [0.0] * 3 + [0.5] * 3 + [0.0] * 5:
        tracker.feed(energy)
    assert tracker.state is State.DONE
    assert tracker.feed(0.9) is State.DONE
