"""voicebox's settings, read once from the environment and validated there.

It embeds the settings every plugin reads the same way - the shared speech server's,
and a voice read under `VOICEBOX_` - and adds its own transcription settings and state
directory. Like `speak_core.config`, it never reads `os.environ` itself.
"""

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from speak_core.config import ServerSettings, VoiceSettings, env_number, env_path, plugin_state_dir

PLUGIN = "voicebox"
PREFIX = "VOICEBOX"

# Reading aloud is what voicebox's voice is for, so it asks for the neural one. The
# `nemo` engine still speaks through `say` while the server warms or when it is absent.
DEFAULT_ENGINE = "nemo"


def _default_speech() -> VoiceSettings:
    return VoiceSettings(prefix=PREFIX, engine=DEFAULT_ENGINE)


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    speech: VoiceSettings = field(default_factory=_default_speech)
    transcribe_dir: Path | None = None
    transcribe_timeout: float | None = None
    state_dir: Path = Path()

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, platform: str | None = None) -> "Settings":
        platform = platform if platform is not None else sys.platform
        return cls(
            server=ServerSettings.from_env(env, platform=platform),
            speech=VoiceSettings.from_env(env, prefix=PREFIX, default_engine=DEFAULT_ENGINE),
            transcribe_dir=env_path(env, "VOICEBOX_TRANSCRIBE_DIR"),
            transcribe_timeout=env_number(env, "VOICEBOX_TRANSCRIBE_TIMEOUT", None),
            state_dir=plugin_state_dir(
                env, name=PLUGIN, override="VOICEBOX_STATE_DIR", platform=platform
            ),
        )
