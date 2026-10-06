"""macOS built-in speech."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from speak.engines.base import Utterance

logger = logging.getLogger(__name__)

# Natural-sounding voices to draw from when the voice is chosen at random.
# `say` also reports novelty, legacy and locale-suffixed voices, which make poor
# defaults for a session that lasts hours.
NATURAL_VOICES = ("Samantha", "Karen", "Moira", "Tessa", "Daniel", "Rishi", "Aman", "Tara")

# Voice names telemetry may record as they are; any other voice is recorded as
# `custom`, because `say` also speaks third-party voices whose names are free text.
KNOWN_VOICES = NATURAL_VOICES


def default_language() -> str:
    return os.environ.get("LANG", "en_US")[:2]


async def run_say_listing() -> str:
    process = await asyncio.create_subprocess_exec(
        "say", "-v", "?", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    stdout, _ = await process.communicate()
    return stdout.decode("utf-8", errors="replace")


def parse_voice_listing(listing: str, language: str | None) -> list[str]:
    """Pull voice names out of `say -v '?'` output.

    Each line is "Name  locale  # sample sentence". Names contain spaces and
    nested parentheses, so the locale is found from the right rather than by
    splitting on whitespace from the left.
    """
    voices: list[str] = []
    for line in listing.splitlines():
        head = line.split("#")[0].strip()
        if not head:
            continue
        name, separator, locale = head.rpartition(" ")
        if not separator:
            continue
        if language and not locale.lower().startswith(language.lower()):
            continue
        voices.append(name.strip())
    return voices


class SayEngine:
    """Speaks through `say`. The text is an argv entry, never a shell string."""

    name = "say"
    random_pool: tuple[str, ...] | None = NATURAL_VOICES

    def __init__(
        self,
        *,
        default_voice: str | None = None,
        run_listing: Callable[[], Awaitable[str]] = run_say_listing,
        language: str | None = None,
    ) -> None:
        self.default_voice = default_voice
        self._run_listing = run_listing
        self._language = language if language is not None else default_language()

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance:
        args: list[str] = []
        if voice:
            args += ["-v", voice]
        if rate:
            args += ["-r", f"{rate:g}"]
        args.append(text)
        return Utterance(command="say", args=args, engine=self.name, voice=voice or None)

    async def list_voices(self) -> list[str]:
        try:
            listing = await self._run_listing()
        except OSError as error:
            logger.warning("could not list say voices: %s", error)
            return []
        return parse_voice_listing(listing, self._language)
