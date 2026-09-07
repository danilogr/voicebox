"""Transcription against the shared speech server's OpenAI-compatible route."""

import logging
from typing import Protocol

import httpx2

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
ASR_CAPABILITY = "transcription"


class Transcriber(Protocol):
    """What a caller needs from transcription. Exists so the tests can fake it."""

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> str: ...

    async def has_asr(self) -> bool: ...


class TranscriptionFailed(RuntimeError):  # noqa: N818 - matches the plugin's other failure names
    """The server could not be reached, or answered with something unusable."""


class NemoTranscriber:
    """Turns recorded audio into text using the server the supervisor manages."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx2.AsyncClient | None = None,
        api_key: str | None = None,
        model: str = "nemotron-3.5",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx2.AsyncClient(timeout=timeout)
        self._model = model
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> str:
        """Transcribe one recording, collapsed to a single line."""
        form = {"model": self._model}
        if language:
            form["language"] = language
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/audio/transcriptions",
                files={"file": ("recording.wav", wav, "audio/wav")},
                data=form,
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            text = response.json().get("text", "")
        except (httpx2.HTTPError, ValueError) as error:
            raise TranscriptionFailed(f"could not transcribe: {error}") from error
        return " ".join(str(text).split())

    async def reachable(self) -> bool:
        """True when a speech server is answering at all.

        Distinct from `has_asr`: a server that is up but was started without a
        transcription model must not be replaced by a second one on the same port.
        """
        try:
            response = await self._client.get(
                f"{self._base_url}/health", headers=self._headers, timeout=self._timeout
            )
        except httpx2.HTTPError:
            return False
        return bool(response.is_success)

    async def has_asr(self) -> bool:
        """True when the running server reports a transcription model."""
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/models", headers=self._headers, timeout=self._timeout
            )
            response.raise_for_status()
            models = response.json().get("data", [])
        except (httpx2.HTTPError, ValueError) as error:
            logger.warning("could not list speech server models: %s", error)
            return False
        return any(model.get("capability") == ASR_CAPABILITY for model in models)
