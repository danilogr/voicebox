"""Transcription against the shared speech server's OpenAI-compatible route."""

import logging
from enum import Enum
from typing import Protocol

import httpx2

logger = logging.getLogger(__name__)

# A backstop, not the operating value: a caller derives its own from the longest
# recording it allows. It has to stay above any plugin's default recording cap,
# because a transcription that times out has already thrown the audio away and
# reports the server's fault instead of its own.
DEFAULT_TIMEOUT = 300.0

# Liveness against localhost: a server that is up answers in milliseconds, and one
# that is wedged must not hold `listen` for the length of a transcription timeout.
PROBE_TIMEOUT = 5.0

ASR_CAPABILITY = "transcription"


class Liveness(Enum):
    """What the speech server's port answers, before asking what it can do.

    Three states rather than two, because a cold start spends several seconds in
    the middle one. A caller that collapses STARTING into ABSENT tells the user to
    reinstall a model they already have, when the answer is to wait.
    """

    ABSENT = "absent"
    STARTING = "starting"
    READY = "ready"


class Transcriber(Protocol):
    """What a caller needs from transcription. Exists so the tests can fake it."""

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> str: ...

    async def has_asr(self) -> bool: ...

    async def reachable(self) -> bool: ...

    async def liveness(self) -> Liveness: ...


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
            payload = response.json()
        except (httpx2.HTTPError, ValueError) as error:
            raise TranscriptionFailed(f"could not transcribe: {error}") from error
        if not isinstance(payload, dict):
            raise TranscriptionFailed(
                f"expected a JSON object from the server, got {type(payload).__name__}"
            )
        return " ".join(str(payload.get("text", "")).split())

    async def liveness(self) -> Liveness:
        """Ask the port what state the speech server is in.

        Callers that are about to use the server need the three-way answer: nothing
        listening, something listening that is not serving yet, and a server that is
        ready. `has_asr` cannot supply it - it answers False for any HTTP failure, so
        a server eight seconds from ready looks exactly like one with no transcription
        model, and every remedy that message names is wrong.
        """
        try:
            response = await self._client.get(
                f"{self._base_url}/health", headers=self._headers, timeout=PROBE_TIMEOUT
            )
        except httpx2.HTTPError:
            return Liveness.ABSENT
        return Liveness.READY if response.is_success else Liveness.STARTING

    async def reachable(self) -> bool:
        """True when something is listening on the speech server's port.

        Liveness, not readiness: any HTTP answer means a process holds the port, so a
        404 or the 503 of a server still warming up counts as reachable. A caller that
        treated those as absence would start a second server on a port already taken.
        Distinct from `has_asr` for the same reason: a server that is up but was
        started without a transcription model must not be replaced either.
        """
        return await self.liveness() is not Liveness.ABSENT

    async def has_asr(self) -> bool:
        """True when the running server reports a transcription model."""
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/models", headers=self._headers, timeout=PROBE_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx2.HTTPError, ValueError) as error:
            logger.warning("could not list speech server models: %s", error)
            return False
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            logger.warning("speech server did not report a usable model list")
            return False
        return any(
            isinstance(model, dict) and model.get("capability") == ASR_CAPABILITY
            for model in models
        )
