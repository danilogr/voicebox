"""This plugin's settings, read once from the environment and validated there.

It embeds the settings every plugin reads the same way - the shared speech server's,
and a voice read under `SPEAK_` - and adds its own listening and transcription
settings. Like `config`, it never reads `os.environ` itself.
"""

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from speak_core.config import (
    ServerSettings,
    VoiceSettings,
    env_flag,
    env_number,
    env_path,
    env_positive,
    env_text,
    plugin_state_dir,
)

PLUGIN = "speak"
PREFIX = "SPEAK"


def _default_speech() -> VoiceSettings:
    return VoiceSettings(prefix=PREFIX)


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    speech: VoiceSettings = field(default_factory=_default_speech)
    listen_silence_ms: float = 3000.0
    listen_max_seconds: float = 240.0
    listen_timeout: float = 10.0
    listen_language: str | None = None
    listen_cue: bool = True
    # None means the sound `cue` picks for itself; only an override lands here.
    listen_cue_start: Path | None = None
    listen_cue_end: Path | None = None
    listen_cue_empty: Path | None = None
    # A named input is never fallen back off; unset means search from the OS default.
    input_device: str | None = None
    transcribe_dir: Path | None = None
    transcribe_timeout: float | None = None
    state_dir: Path = Path()

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, platform: str | None = None) -> "Settings":
        platform = platform if platform is not None else sys.platform
        return cls(
            server=ServerSettings.from_env(env, platform=platform),
            speech=VoiceSettings.from_env(env, prefix=PREFIX),
            listen_silence_ms=env_positive(env, "SPEAK_LISTEN_SILENCE_MS", 3000.0),
            listen_max_seconds=env_positive(env, "SPEAK_LISTEN_MAX_SECONDS", 240.0),
            listen_timeout=env_positive(env, "SPEAK_LISTEN_TIMEOUT", 10.0),
            listen_language=env_text(env, "SPEAK_LISTEN_LANGUAGE"),
            listen_cue=env_flag(env, "SPEAK_LISTEN_CUE", default=True),
            listen_cue_start=env_path(env, "SPEAK_LISTEN_CUE_START"),
            listen_cue_end=env_path(env, "SPEAK_LISTEN_CUE_END"),
            listen_cue_empty=env_path(env, "SPEAK_LISTEN_CUE_EMPTY"),
            input_device=env_text(env, "SPEAK_INPUT_DEVICE"),
            transcribe_dir=env_path(env, "SPEAK_TRANSCRIBE_DIR"),
            transcribe_timeout=env_number(env, "SPEAK_TRANSCRIBE_TIMEOUT", None),
            state_dir=plugin_state_dir(
                env, name=PLUGIN, override="SPEAK_STATE_DIR", platform=platform
            ),
        )
