"""Settings every plugin reads the same way, validated where they enter.

Two kinds. The speech server's settings are shared and unprefixed, because whichever
plugin starts the server decides its arguments for every plugin that uses it. A voice
belongs to one plugin and is read under the prefix that plugin names, so one plugin
can speak through `say` while another reads aloud in a neural voice.

Nothing here reads `os.environ`: every function takes the mapping its caller read.
Values that do not parse fall back to their default with a warning, never an error.
"""

import logging
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from speak.text import DEFAULT_MAX_CHARS

logger = logging.getLogger(__name__)

DEFAULT_NEMO_URL = "http://127.0.0.1:8082"
DEFAULT_NEMO_PORT = 8082
RANDOM_VOICE = "random"

# Where everything the plugins must agree on lives: the speech server's start lock,
# its pid record, its client registry, and the speaker lock. These files' formats are
# a contract between speak-core versions; a release that changes one must change this
# name, so an older and a newer plugin never misread each other.
SHARED_STATE_DIRNAME = "speak-core"
SHARED_STATE_OVERRIDE = "SPEAK_CORE_STATE_DIR"

TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})


def env_text(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key, "").strip()
    return value or None


def env_number(env: Mapping[str, str], key: str, default: float | None) -> float | None:
    raw = env_text(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring %s=%r: not a number", key, raw)
        return default
    if not math.isfinite(value):
        # nan passes every `<= 0` check downstream and int(nan) raises, so it is
        # rejected here rather than left to whichever caller trips over it first.
        logger.warning("ignoring %s=%r: not a finite number", key, raw)
        return default
    return value


def env_flag(env: Mapping[str, str], key: str, *, default: bool = False) -> bool:
    """A yes/no setting. Anything unrecognised, the empty string included, is the default."""
    raw = (env_text(env, key) or "").lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return default


def env_path(env: Mapping[str, str], key: str) -> Path | None:
    raw = env_text(env, key)
    return None if raw is None else Path(raw)


def env_positive(env: Mapping[str, str], key: str, default: float) -> float:
    value = env_number(env, key, default)
    if value is None or value <= 0:
        logger.warning("ignoring %s: must be a positive number", key)
        return default
    return value


def env_list(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    """A comma-separated list, with spacing and empty entries dropped."""
    raw = env_text(env, key) or ""
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def cache_root(env: Mapping[str, str], *, platform: str) -> Path:
    """The per-user cache directory every state directory lives under."""
    home = Path(env.get("HOME", "~")).expanduser()
    if platform == "darwin":
        return home / "Library" / "Caches"
    return Path(env.get("XDG_CACHE_HOME") or home / ".cache")


def plugin_state_dir(env: Mapping[str, str], *, name: str, override: str, platform: str) -> Path:
    """One plugin's own state directory: `<cache>/<name>`, unless `override` names one."""
    raw = env_text(env, override)
    return Path(raw) if raw else cache_root(env, platform=platform) / name


def shared_state_dir(env: Mapping[str, str], *, platform: str) -> Path:
    """The directory every plugin shares: `<cache>/speak-core`, or `SPEAK_CORE_STATE_DIR`."""
    raw = env_text(env, SHARED_STATE_OVERRIDE)
    return Path(raw) if raw else cache_root(env, platform=platform) / SHARED_STATE_DIRNAME


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """The one speech server every plugin shares, and where they coordinate over it."""

    url: str = DEFAULT_NEMO_URL
    api_key: str | None = None
    tts_model: str = "magpie"
    asr_model: str = "nemotron-3.5"
    binary: str = "nemo-speech"
    autostart: bool = True
    seed: int | None = None
    shared_dir: Path = Path()

    @property
    def port(self) -> int:
        return urlparse(self.url).port or DEFAULT_NEMO_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, platform: str | None = None) -> "ServerSettings":
        seed = env_number(env, "NEMO_TTS_SEED", None)
        return cls(
            url=env_text(env, "NEMO_HTTP_URL") or DEFAULT_NEMO_URL,
            api_key=env_text(env, "NEMO_API_KEY"),
            tts_model=env_text(env, "NEMO_TTS_MODEL") or "magpie",
            asr_model=env_text(env, "NEMO_ASR_MODEL") or "nemotron-3.5",
            binary=env_text(env, "NEMO_BIN") or "nemo-speech",
            autostart=env_flag(env, "NEMO_AUTOSTART", default=True),
            seed=None if seed is None else int(seed),
            shared_dir=shared_state_dir(
                env, platform=platform if platform is not None else sys.platform
            ),
        )


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """How one plugin speaks, read under that plugin's prefix (`<PREFIX>_ENGINE`, ...)."""

    prefix: str
    engine: str = "say"
    voice: str | None = None
    randomize_voice: bool = False
    voice_pool: tuple[str, ...] = ()
    rate: float | None = None
    max_chars: int = DEFAULT_MAX_CHARS
    command: str | None = None

    def key(self, name: str) -> str:
        """The variable this plugin reads `name` from, for messages that name it."""
        return f"{self.prefix}_{name}"

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, prefix: str) -> "VoiceSettings":
        def key(name: str) -> str:
            return f"{prefix}_{name}"

        cap = env_number(env, key("MAX_CHARS"), DEFAULT_MAX_CHARS)
        if cap is None or cap < 1:
            if cap is not None:
                logger.warning("ignoring %s=%s: must be positive", key("MAX_CHARS"), cap)
            cap = DEFAULT_MAX_CHARS

        wanted = env_text(env, key("VOICE"))
        randomize = (wanted or "").lower() == RANDOM_VOICE
        return cls(
            prefix=prefix,
            engine=(env_text(env, key("ENGINE")) or "say").lower(),
            voice=None if randomize else wanted,
            randomize_voice=randomize,
            voice_pool=env_list(env, key("VOICE_POOL")),
            rate=env_number(env, key("RATE"), None),
            max_chars=int(cap),
            command=env_text(env, key("COMMAND")),
        )
