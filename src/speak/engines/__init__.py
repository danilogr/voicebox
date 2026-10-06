"""Speech engines: everything that turns a sentence into a command to run."""

import logging
import sys
from collections.abc import Callable

from speak.config import ServerSettings, VoiceSettings
from speak.engines.base import Engine, Utterance
from speak.engines.command import CommandEngine
from speak.engines.nemo import NemoEngine
from speak.engines.say import SayEngine

logger = logging.getLogger(__name__)

__all__ = ["CommandEngine", "Engine", "NemoEngine", "SayEngine", "Utterance", "build_engine"]


def build_engine(
    voice: VoiceSettings,
    server: ServerSettings,
    *,
    on_unavailable: Callable[[], None] | None,
    platform: str | None = None,
) -> Engine:
    """Pick the engine the plugin's voice settings name, falling back to `say`.

    Messages name the plugin's own variables (`<PREFIX>_ENGINE`, ...).
    """
    platform = platform if platform is not None else sys.platform
    if voice.engine == "command":
        if not (voice.command or "").split():
            raise ValueError(
                f"{voice.key('COMMAND')} must be set when {voice.key('ENGINE')}=command"
            )
        return CommandEngine(voice.command, default_voice=voice.voice)

    if voice.engine == "nemo":
        return NemoEngine(
            base_url=server.url,
            api_key=server.api_key,
            model=server.tts_model,
            default_voice=voice.voice or "John",
            fallback=SayEngine(),
            on_unavailable=on_unavailable,
        )

    if voice.engine != "say":
        logger.warning('unknown %s "%s", using say', voice.key("ENGINE"), voice.engine)

    if platform != "darwin":
        # `say` ships with macOS and nowhere else. Saying so once at startup beats a
        # warning per sentence from the queue, whose message is only "no such file".
        logger.warning(
            "the say engine needs macOS; on this platform set %s=nemo for the local "
            "speech server, or %s=command with %s=<binary>",
            voice.key("ENGINE"),
            voice.key("ENGINE"),
            voice.key("COMMAND"),
        )

    return SayEngine(default_voice=voice.voice)
