"""Finding something on this machine that can play a WAV file.

Playback is a subprocess rather than decoded in this process, because the speech
queue stops an utterance by terminating the player. So what is resolved here is a
command, and the only format every candidate reads is 16-bit PCM WAV - which is
what both the shipped cues and the speech server's audio are.
"""

import logging
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# PowerShell takes the file inside a script rather than as a trailing argument,
# which is why `script` exists at all. SoundPlayer plays PCM WAV and nothing else.
_POWERSHELL = "(New-Object Media.SoundPlayer '{path}').PlaySync()"
_PS_ARGS = ("-NoProfile", "-NonInteractive", "-Command")


@dataclass(frozen=True, slots=True)
class Player:
    """A command that plays one sound file and exits when it has finished."""

    binary: str
    args: tuple[str, ...] = ()
    script: str | None = None

    def command_for(self, path: str) -> tuple[str, list[str]]:
        """The binary and arguments that play `path`, ready for a spawn."""
        if self.script is None:
            return self.binary, [*self.args, path]
        # Single quotes delimit the path in the script, so a quote in the path has
        # to be doubled or it ends the string and the rest becomes code.
        return self.binary, [*self.args, self.script.format(path=path.replace("'", "''"))]


# Per platform first, then the portable ones. A mac without afplay is not a thing,
# but trying the rest afterwards costs nothing and covers an unusual box.
_PLATFORM: dict[str, tuple[Player, ...]] = {
    "darwin": (Player("afplay"),),
    "win32": (
        Player("powershell", _PS_ARGS, _POWERSHELL),
        Player("pwsh", _PS_ARGS, _POWERSHELL),
    ),
}

# PulseAudio or PipeWire first, then ALSA, then the media players people already have.
_PORTABLE: tuple[Player, ...] = (
    Player("paplay"),
    Player("aplay", ("-q",)),
    Player("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")),
    Player("play", ("-q",)),
    Player("mpv", ("--no-video", "--really-quiet")),
)


def candidates(platform: str) -> tuple[Player, ...]:
    """Every player worth trying on this platform, best first."""
    return (*_PLATFORM.get(platform, ()), *_PORTABLE)


def resolve_player(
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Player | None:
    """The first player on this machine that exists, or None if there is none.

    `which` is injected so the search is testable without installing anything, and
    because what matters is which binaries are present rather than how they are found.
    """
    for candidate in candidates(platform if platform is not None else sys.platform):
        if which(candidate.binary) is not None:
            return candidate
    logger.debug("no sound player found; cues and server speech cannot be played")
    return None
