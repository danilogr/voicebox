"""Configuration, read once from the environment and validated at that boundary."""

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from speak.text import DEFAULT_MAX_CHARS

logger = logging.getLogger(__name__)

DEFAULT_NEMO_URL = "http://127.0.0.1:8082"
DEFAULT_NEMO_PORT = 8082


def _text(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key, "").strip()
    return value or None


def _number(env: Mapping[str, str], key: str, default: float | None) -> float | None:
    raw = _text(env, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring %s=%r: not a number", key, raw)
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    engine: str = "say"
    voice: str | None = None
    rate: float | None = None
    max_chars: int = DEFAULT_MAX_CHARS
    command: str | None = None
    nemo_url: str = DEFAULT_NEMO_URL
    nemo_api_key: str | None = None
    nemo_model: str = "magpie"
    nemo_binary: str = "nemo-speech"
    nemo_autostart: bool = True
    seed: int | None = None
    state_dir: Path = Path()

    @property
    def nemo_port(self) -> int:
        return urlparse(self.nemo_url).port or DEFAULT_NEMO_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, platform: str | None = None) -> "Settings":
        cap = _number(env, "SPEAK_MAX_CHARS", DEFAULT_MAX_CHARS)
        if cap is None or cap < 1:
            if cap is not None:
                logger.warning("ignoring SPEAK_MAX_CHARS=%s: must be positive", cap)
            cap = DEFAULT_MAX_CHARS

        seed = _number(env, "SPEAK_SEED", None)

        return cls(
            engine=(_text(env, "SPEAK_ENGINE") or "say").lower(),
            voice=_text(env, "SPEAK_VOICE"),
            rate=_number(env, "SPEAK_RATE", None),
            max_chars=int(cap),
            command=_text(env, "SPEAK_COMMAND"),
            nemo_url=_text(env, "NEMO_HTTP_URL") or DEFAULT_NEMO_URL,
            nemo_api_key=_text(env, "NEMO_API_KEY"),
            nemo_model=_text(env, "NEMO_TTS_MODEL") or "magpie",
            nemo_binary=_text(env, "SPEAK_NEMO_BIN") or "nemo-speech",
            nemo_autostart=_text(env, "SPEAK_NEMO_AUTOSTART") != "0",
            seed=None if seed is None else int(seed),
            state_dir=_state_dir(env, platform if platform is not None else sys.platform),
        )


def _state_dir(env: Mapping[str, str], platform: str) -> Path:
    override = _text(env, "SPEAK_STATE_DIR")
    if override:
        return Path(override)

    home = Path(env.get("HOME", "~")).expanduser()
    if platform == "darwin":
        return home / "Library" / "Caches" / "speak"
    return Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "speak"
