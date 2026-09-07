"""Wiring shared by the two entry points: logging, the health probe, capture limits."""

import asyncio
import logging
import sys

import httpx2

from speak.capture import TrackerSettings
from speak.config import Settings
from speak.supervisor import Probe

HEALTH_TIMEOUT = 30.0
HEALTH_INTERVAL = 0.5


def configure_logging() -> None:
    """Log to stderr only. stdout carries the protocol and nothing else."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[speak] %(message)s"))
    root = logging.getLogger("speak")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def health_probe(base_url: str, timeout: float = HEALTH_TIMEOUT) -> Probe:
    """Poll until the speech server answers, so the start lock covers the real warmup."""

    async def probe() -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx2.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(f"{base_url.rstrip('/')}/health")
                except httpx2.HTTPError:
                    pass
                else:
                    if response.is_success:
                        return True
                await asyncio.sleep(HEALTH_INTERVAL)
        return False

    return probe


def capture_limits(settings: Settings) -> TrackerSettings:
    """Turn the configured seconds and milliseconds into one recording's limits."""
    return TrackerSettings(
        silence_ms=settings.listen_silence_ms,
        start_timeout_ms=settings.listen_timeout * 1000,
        max_ms=settings.listen_max_seconds * 1000,
    )
