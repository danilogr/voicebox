"""Who a plugin's telemetry is: the names it reads, where it keeps its consent, where
it sends its data, and which disclosure its users were shown.

Every variable is derived from one prefix, so one plugin's switches never reach
another's: `VOICEBOX_TELEMETRY=off` says nothing about `walkie`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TelemetryIdentity:
    # Names the config directory and the consent command's messages.
    plugin: str
    # A prefix P gives P_TELEMETRY, P_TELEMETRY_DEBUG, P_TELEMETRY_ENDPOINT and
    # P_HARNESS.
    env_prefix: str
    # The ingest URL. The install ping is served from the same origin, so an override
    # of this moves both.
    default_endpoint: str
    # Which version of the plugin's TELEMETRY.md describes what an install sends.
    disclosure_version: int

    @property
    def mode_env(self) -> str:
        return f"{self.env_prefix}_TELEMETRY"

    @property
    def debug_env(self) -> str:
        return f"{self.env_prefix}_TELEMETRY_DEBUG"

    @property
    def endpoint_env(self) -> str:
        return f"{self.env_prefix}_TELEMETRY_ENDPOINT"

    @property
    def harness_env(self) -> str:
        return f"{self.env_prefix}_HARNESS"

    def config_dir(self, env: Mapping[str, str], *, host_platform: str) -> Path:
        """Where consent and the cached machine probe live.

        The OS's per-user config directory, never a cache: a cache the OS may clear
        would silently re-enable telemetry someone had switched off.
        """
        home = Path(env.get("HOME", "~")).expanduser()
        if host_platform == "darwin":
            return home / "Library" / "Application Support" / self.plugin
        base = env.get("XDG_CONFIG_HOME")
        return (Path(base) if base else home / ".config") / self.plugin

    def ingest_url(self, env: Mapping[str, str]) -> str:
        return env.get(self.endpoint_env) or self.default_endpoint

    def debug_requested(self, env: Mapping[str, str]) -> bool:
        """Log what would be sent instead of sending it."""
        return (env.get(self.debug_env) or "").strip() not in ("", "0")
