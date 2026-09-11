"""Speech engines: everything that turns a sentence into a command to run."""

import logging
import sys
from collections.abc import Callable

from speak.config import Settings
from speak.engines.base import Engine, Utterance
from speak.engines.command import CommandEngine
from speak.engines.nemo import NemoEngine
from speak.engines.say import SayEngine

logger = logging.getLogger(__name__)

__all__ = ["CommandEngine", "Engine", "NemoEngine", "SayEngine", "Utterance", "build_engine"]


def build_engine(
    settings: Settings,
    *,
    on_unavailable: Callable[[], None] | None,
    platform: str | None = None,
) -> Engine:
    """Pick the engine named in the settings, falling back to `say`."""
    platform = platform if platform is not None else sys.platform
    if settings.engine == "command":
        return CommandEngine(settings.command, default_voice=settings.voice)

    if settings.engine == "nemo":
        return NemoEngine(
            base_url=settings.nemo_url,
            api_key=settings.nemo_api_key,
            model=settings.nemo_model,
            default_voice=settings.voice or "John",
            fallback=SayEngine(),
            on_unavailable=on_unavailable,
        )

    if settings.engine != "say":
        logger.warning('unknown SPEAK_ENGINE "%s", using say', settings.engine)

    if platform != "darwin":
        # `say` ships with macOS and nowhere else. Saying so once at startup beats a
        # warning per sentence from the queue, whose message is only "no such file".
        logger.warning(
            "the say engine needs macOS; on this platform set SPEAK_ENGINE=nemo for the "
            "local speech server, or SPEAK_ENGINE=command with SPEAK_COMMAND=<binary>"
        )

    return SayEngine(default_voice=settings.voice)
