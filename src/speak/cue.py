"""The two chimes that bracket one recording.

Nothing else tells the user the microphone is open, and because recording stops on
silence rather than on a keypress, nothing else tells them it has closed either.
Both cues are awaited rather than left playing: `capture.SilenceTracker` calibrates
its noise gate from the first 300 ms of audio, so a chime still sounding when the
stream opens is averaged into that gate and the recording ends in silence.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from speak.queue import Spawn, spawn_detached

logger = logging.getLogger(__name__)

PLAYER = "/usr/bin/afplay"

# macOS system sounds, and the reason the paths are configurable: Tink is bright and
# half a second long, Purr is softer and lower, so the two are told apart without
# being looked at.
START_SOUND = Path("/System/Library/Sounds/Tink.aiff")
END_SOUND = Path("/System/Library/Sounds/Purr.aiff")
# Recording stops on silence, so a capture that caught nothing ends exactly like one
# that worked. Basso is the macOS error tone and is not mistakable for Purr, which
# is why it replaces the closing chime rather than following it: hearing "got it"
# and then "that failed" is worse than hearing only the second.
EMPTY_SOUND = Path("/System/Library/Sounds/Basso.aiff")


@dataclass(slots=True)
class Outcome:
    """Whether the bracketed recording caught any speech; set by the body."""

    heard: bool = False


class Cue:
    """Sounds one chime as the microphone opens and another as it closes.

    A cue is a convenience and a recording is not: a missing sound file, a missing
    player, or a player that exits non-zero is logged at debug and the recording goes
    ahead regardless.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        start: Path | None = None,
        end: Path | None = None,
        empty: Path | None = None,
        player: str = PLAYER,
        spawn: Spawn = spawn_detached,
    ) -> None:
        # The paths default here rather than in `config`, so an unset override can be
        # carried straight through from the environment to this one place.
        self._enabled = enabled
        self._start = start if start is not None else START_SOUND
        self._end = end if end is not None else END_SOUND
        self._empty = empty if empty is not None else EMPTY_SOUND
        self._player = player
        self._spawn = spawn

    @asynccontextmanager
    async def around_recording(self) -> AsyncIterator[Outcome]:
        """Chime, record, then chime or buzz depending on what the body heard.

        The opening cue has finished before the body runs, which is what keeps it out
        of the noise gate. The closing one plays whatever the body did, so a recording
        that fails still closes the bracket it opened - and because the outcome starts
        False, a body that raised reports the failure sound without having to say so.
        """
        await self._play(self._start)
        outcome = Outcome()
        try:
            yield outcome
        finally:
            await self._play(self._end if outcome.heard else self._empty)

    async def _play(self, sound: Path) -> None:
        if not self._enabled:
            return
        try:
            player = await self._spawn(self._player, [str(sound)])
            code = await player.wait()
        except OSError as error:
            logger.debug("no cue played: %s", error)
            return
        if code != 0:
            logger.debug("cue player exited %s for %s", code, sound)
