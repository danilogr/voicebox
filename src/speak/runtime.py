"""Wiring shared by the two entry points: logging, the health probe, capture, cues."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable

import httpx2

from speak.capture import MicrophoneRecorder, TrackerSettings
from speak.config import ServerSettings
from speak.cue import Cue
from speak.settings import Settings
from speak.supervisor import NemoSupervisor, Probe, Spawn, process_is_alive, spawn_server

HEALTH_TIMEOUT = 30.0
HEALTH_INTERVAL = 0.5

# Enough for the request itself on top of the audio. Transcription runs well above
# realtime (see harness/performance.md), so a second of allowance per second of
# recording is generous.
TRANSCRIBE_MARGIN = 30.0


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


def transcription_timeout(settings: Settings) -> float:
    """How long one transcription may take, derived from the longest recording allowed.

    A fixed timeout cannot serve a configurable cap: at the default 240 seconds the
    old 30-second one could not have transcribed a one-minute answer, and a
    transcription that times out has already discarded the audio and blames the
    server rather than reporting that it gave up.
    """
    return settings.listen_max_seconds + TRANSCRIBE_MARGIN


def capture_limits(settings: Settings) -> TrackerSettings:
    """Turn the configured seconds and milliseconds into one recording's limits."""
    return TrackerSettings(
        silence_ms=settings.listen_silence_ms,
        start_timeout_ms=settings.listen_timeout * 1000,
        max_ms=settings.listen_max_seconds * 1000,
    )


def build_cue(settings: Settings) -> Cue:
    """The chimes that bracket one recording, built the same way by both entry points."""
    return Cue(
        enabled=settings.listen_cue,
        start=settings.listen_cue_start,
        end=settings.listen_cue_end,
        empty=settings.listen_cue_empty,
    )


def build_recorder(settings: Settings) -> MicrophoneRecorder:
    """The microphone, built the same way by both entry points."""
    return MicrophoneRecorder(device=settings.input_device)


def supervisor_for(
    server: ServerSettings,
    *,
    spawn: Spawn = spawn_server,
    probe: Probe | None = None,
    pid: int | None = None,
    is_alive: Callable[[int], bool] = process_is_alive,
    kill: Callable[[int, int], None] = os.kill,
) -> NemoSupervisor:
    """The supervisor for the one shared speech server, built from the shared settings.

    Only `ServerSettings` goes in, so the server a plugin would start is the same
    whichever plugin starts it: it requests both models, the shared port and seed, and its lock,
    pid record and client registry in the shared state directory.
    """
    return NemoSupervisor(
        state_dir=server.shared_dir,
        spawn=spawn,
        probe=probe if probe is not None else health_probe(server.url),
        port=server.port,
        binary=server.binary,
        model=server.tts_model,
        asr_model=server.asr_model,
        seed=server.seed,
        pid=pid,
        is_alive=is_alive,
        kill=kill,
    )
