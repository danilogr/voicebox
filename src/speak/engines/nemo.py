"""A local NeMo-Speech.cpp server, or any OpenAI-compatible /v1/audio/speech endpoint."""

import asyncio
import io
import logging
import tempfile
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

import httpx2

from speak.engines.base import Engine, Utterance
from speak.engines.say import SayEngine
from speak.player import Player, resolve_player

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0

# The speakers of the MagpieTTS model the plugin installs. Telemetry records these
# names as they are and any other voice - another model's, or another
# OpenAI-compatible server's - as `custom`, so a speaker added by a new model shows
# up as `custom` until it is listed here.
KNOWN_VOICES = ("John", "Sofia", "Aria", "Jason", "Leo")


def _wav_duration_ms(wav: bytes) -> int | None:
    """The audio's own length, read from the WAV header already in memory.

    Best-effort: a server that returns something other than a well-formed WAV
    still gets spoken, just without this one figure.
    """
    try:
        with wave.open(io.BytesIO(wav)) as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (wave.Error, EOFError):
        return None
    return round(frames / rate * 1000) if rate > 0 else None


class NemoEngine:
    """Synthesises to a WAV file and plays it.

    When the server cannot answer, the sentence is handed to the fallback engine
    instead of being lost, and `on_unavailable` is invoked so the server can be
    started for the next one.
    """

    name = "nemo"
    random_pool: tuple[str, ...] | None = None

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx2.AsyncClient | None = None,
        api_key: str | None = None,
        model: str = "magpie",
        default_voice: str | None = "John",
        timeout: float = DEFAULT_TIMEOUT,
        fallback: Engine | None = None,
        on_unavailable: Callable[[], None] | None = None,
        player: Player | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx2.AsyncClient(timeout=timeout)
        self._model = model
        self.default_voice = default_voice
        self._timeout = timeout
        self._fallback = fallback or SayEngine()
        self._player = player if player is not None else resolve_player()
        self._on_unavailable = on_unavailable
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance:
        try:
            audio = await self._synthesize(text, voice)
        except (httpx2.HTTPError, OSError) as error:
            logger.warning("speech server unavailable, using say instead: %s", error)
            if self._on_unavailable is not None:
                self._on_unavailable()
            return await self._fallback.prepare(text, voice=None, rate=rate)

        if self._player is None:
            # Audio we cannot play is worse than no audio: the sentence would be
            # silently lost. The fallback speaks it instead.
            logger.warning("no sound player on this machine, using say instead")
            return await self._fallback.prepare(text, voice=None, rate=rate)

        path = Path(tempfile.gettempdir()) / f"speak-{uuid.uuid4().hex}.wav"
        await asyncio.to_thread(path.write_bytes, audio)

        async def cleanup() -> None:
            await asyncio.to_thread(path.unlink, True)

        command, args = self._player.command_for(str(path))
        return Utterance(
            command=command,
            args=args,
            cleanup=cleanup,
            audio_ms=_wav_duration_ms(audio),
            engine=self.name,
            voice=voice or self.default_voice,
        )

    async def _synthesize(self, text: str, voice: str | None) -> bytes:
        response = await self._client.post(
            f"{self._base_url}/v1/audio/speech",
            json={
                "model": self._model,
                "input": text,
                "voice": voice or self.default_voice,
                "response_format": "wav",
            },
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.content

    async def list_voices(self) -> list[str]:
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/models", headers=self._headers, timeout=self._timeout
            )
            response.raise_for_status()
            models = response.json().get("data", [])
        except (httpx2.HTTPError, ValueError) as error:
            logger.warning("could not list speech server voices: %s", error)
            return []

        for model in models:
            if model.get("capability") == "speech":
                return list(model.get("voices", []))
        return []
