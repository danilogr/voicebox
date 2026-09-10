"""The chimes that bracket a recording. Nothing here plays a sound."""

import logging
from pathlib import Path

import pytest

from fakes import FakePlayer
from speak.cue import EMPTY_SOUND, END_SOUND, PLAYER, START_SOUND, Cue

START = Path("/sounds/cue-start.aiff")
END = Path("/sounds/cue-end.aiff")
EMPTY = Path("/sounds/cue-empty.aiff")


def build(player: FakePlayer, *, enabled: bool = True) -> Cue:
    return Cue(enabled=enabled, start=START, end=END, empty=EMPTY, spawn=player.spawn)


async def test_the_opening_cue_is_over_before_the_recording_starts():
    """`SilenceTracker` calibrates its gate from the first 300 ms of audio, so a cue
    still sounding when the stream opens is averaged into the gate and the recording
    ends in silence."""
    calls: list[str] = []
    async with build(FakePlayer(calls)).around_recording() as outcome:
        calls.append("record")
        outcome.heard = True
    assert calls == ["cue-start", "record", "cue-end"]


async def test_both_sounds_go_to_the_player():
    player = FakePlayer()
    async with build(player).around_recording() as outcome:
        outcome.heard = True
    assert player.commands == [(PLAYER, [str(START)]), (PLAYER, [str(END)])]


async def test_the_system_sounds_are_the_defaults():
    player = FakePlayer()
    async with Cue(spawn=player.spawn).around_recording() as outcome:
        outcome.heard = True
    assert player.commands == [(PLAYER, [str(START_SOUND)]), (PLAYER, [str(END_SOUND)])]


async def test_a_capture_that_heard_nothing_buzzes_instead_of_chiming():
    """Recording stops on silence either way, so without a different sound an empty
    capture is indistinguishable from one that worked until the transcript is due."""
    calls: list[str] = []
    async with build(FakePlayer(calls)).around_recording():
        calls.append("record")
    assert calls == ["cue-start", "record", "cue-empty"]


async def test_the_error_sound_is_the_default_for_an_empty_capture():
    player = FakePlayer()
    async with Cue(spawn=player.spawn).around_recording():
        pass
    assert player.commands == [(PLAYER, [str(START_SOUND)]), (PLAYER, [str(EMPTY_SOUND)])]


async def test_a_recording_that_failed_reports_the_failure_sound():
    """The outcome starts False, so a body that raised buzzes without having to say
    so - and a bracket left open would say nothing about when the microphone shut."""
    calls: list[str] = []
    with pytest.raises(RuntimeError):
        async with build(FakePlayer(calls)).around_recording():
            raise RuntimeError("the microphone failed")
    assert calls == ["cue-start", "cue-empty"]


async def test_a_missing_player_does_not_cost_the_recording(caplog):
    calls: list[str] = []
    player = FakePlayer(calls, error=FileNotFoundError(2, "No such file", PLAYER))
    with caplog.at_level(logging.DEBUG, logger="speak.cue"):
        async with build(player).around_recording():
            calls.append("record")
    assert calls == ["record"]
    assert "No such file" in caplog.text


async def test_a_player_that_exits_non_zero_does_not_cost_the_recording(caplog):
    """What a missing sound file looks like from here: `afplay` runs and fails."""
    calls: list[str] = []
    with caplog.at_level(logging.DEBUG, logger="speak.cue"):
        async with build(FakePlayer(calls, code=1)).around_recording() as outcome:
            calls.append("record")
            outcome.heard = True
    assert calls == ["cue-start", "record", "cue-end"]
    assert "cue-start" in caplog.text


async def test_a_cue_that_is_turned_off_plays_nothing():
    calls: list[str] = []
    player = FakePlayer(calls)
    async with build(player, enabled=False).around_recording():
        calls.append("record")
    assert calls == ["record"]
    assert player.commands == []
