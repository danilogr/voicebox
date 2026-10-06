import httpx2
import pytest

from speak.engines.asr import (
    DEFAULT_TIMEOUT,
    PROBE_TIMEOUT,
    Liveness,
    NemoTranscriber,
    TranscriptionFailed,
)
from speak.settings import Settings

MODELS_WITH_ASR = {
    "data": [
        {"id": "nemotron-3.5", "capability": "transcription"},
        {"id": "magpie", "capability": "speech", "voices": ["John"]},
    ]
}
MODELS_WITHOUT_ASR = {"data": [{"id": "magpie", "capability": "speech", "voices": ["John"]}]}
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt "


def transcriber_over(handler, **kwargs: object) -> NemoTranscriber:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return NemoTranscriber(base_url="http://127.0.0.1:8082", client=client, **kwargs)


async def test_returns_the_transcript_collapsed_to_one_line():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"text": "  run   the\ntests  "})

    assert await transcriber_over(handler).transcribe(WAV) == "run the tests"


async def test_posts_the_audio_and_the_model_to_the_transcription_route():
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx2.Response(200, json={"text": "ok"})

    await transcriber_over(handler, model="parakeet-tdt").transcribe(WAV, language="en")
    assert seen["url"].endswith("/v1/audio/transcriptions")
    assert b"parakeet-tdt" in seen["body"]
    assert b'name="language"' in seen["body"]
    assert WAV in seen["body"]


async def test_language_is_omitted_when_unset():
    seen: dict[str, bytes] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = request.content
        return httpx2.Response(200, json={"text": "ok"})

    await transcriber_over(handler).transcribe(WAV)
    assert b'name="language"' not in seen["body"]


async def test_an_empty_transcript_is_returned_as_empty():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"text": ""})

    assert await transcriber_over(handler).transcribe(WAV) == ""


async def test_a_server_error_becomes_a_transcription_failure():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="boom")

    with pytest.raises(TranscriptionFailed):
        await transcriber_over(handler).transcribe(WAV)


async def test_a_non_json_body_becomes_a_transcription_failure():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>nope</html>")

    with pytest.raises(TranscriptionFailed):
        await transcriber_over(handler).transcribe(WAV)


async def test_a_json_body_that_is_not_an_object_becomes_a_transcription_failure():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[1, 2])

    with pytest.raises(TranscriptionFailed):
        await transcriber_over(handler).transcribe(WAV)


async def test_sends_the_api_key_as_a_bearer_token_on_every_route():
    seen: dict[str, str | None] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen[request.url.path] = request.headers.get("authorization")
        return httpx2.Response(200, json={"text": "ok", "data": []})

    transcriber = transcriber_over(handler, api_key="secret")
    await transcriber.transcribe(WAV)
    await transcriber.reachable()
    await transcriber.has_asr()
    assert seen == {
        "/v1/audio/transcriptions": "Bearer secret",
        "/health": "Bearer secret",
        "/v1/models": "Bearer secret",
    }


async def test_has_asr_is_true_when_the_server_reports_a_transcription_model():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=MODELS_WITH_ASR)

    assert await transcriber_over(handler).has_asr() is True


async def test_has_asr_is_false_when_the_server_only_speaks():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=MODELS_WITHOUT_ASR)

    assert await transcriber_over(handler).has_asr() is False


async def test_has_asr_is_false_when_the_server_cannot_be_reached():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    assert await transcriber_over(handler).has_asr() is False


@pytest.mark.parametrize("payload", [{"data": "nope"}, {"data": ["whisper"]}, [1, 2]])
async def test_has_asr_is_false_when_the_model_list_is_malformed(payload):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    assert await transcriber_over(handler).has_asr() is False


async def test_reachable_is_true_when_the_server_answers_its_health_route():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/health"
        return httpx2.Response(200, text="ok")

    assert await transcriber_over(handler).reachable() is True


@pytest.mark.parametrize("status", [404, 503])
async def test_reachable_is_true_when_a_server_answers_with_an_error_status(status):
    """A warming-up or route-less server still holds the port: do not start a second one."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text="not ready")

    assert await transcriber_over(handler).reachable() is True


async def test_reachable_is_false_when_nothing_is_listening():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    assert await transcriber_over(handler).reachable() is False


def read_timeout(request: httpx2.Request) -> float:
    """The deadline httpx applied to this request, as the transport sees it."""
    return request.extensions["timeout"]["read"]


async def test_the_transcription_request_carries_the_timeout_it_was_given():
    """MockTransport answers instantly, so only the deadline itself can be checked."""
    seen: dict[str, float] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["timeout"] = read_timeout(request)
        return httpx2.Response(200, json={"text": "ok"})

    await transcriber_over(handler, timeout=270.0).transcribe(WAV)
    assert seen["timeout"] == 270.0


async def test_the_probes_do_not_wait_as_long_as_a_transcription():
    """A wedged server would otherwise hold `listen` for the length of a recording."""
    seen: dict[str, float] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen[request.url.path] = read_timeout(request)
        return httpx2.Response(200, json={"data": []})

    transcriber = transcriber_over(handler, timeout=270.0)
    await transcriber.liveness()
    await transcriber.has_asr()
    assert seen == {"/health": PROBE_TIMEOUT, "/v1/models": PROBE_TIMEOUT}


def test_the_default_timeout_outlasts_the_longest_recording_allowed():
    """Both entry points derive their own, but a caller that does not must not get a
    timeout the configured cap can walk straight through."""
    assert Settings().listen_max_seconds < DEFAULT_TIMEOUT


async def test_liveness_is_ready_when_the_server_answers_with_success():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="ok")

    assert await transcriber_over(handler).liveness() is Liveness.READY


@pytest.mark.parametrize("status", [404, 503])
async def test_liveness_is_starting_when_the_server_answers_without_success(status):
    """A cold start holds the port before it serves. Callers about to use the server
    need that apart from absence: the remedy is to wait, not to install anything."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text="not ready")

    assert await transcriber_over(handler).liveness() is Liveness.STARTING


async def test_liveness_is_absent_when_nothing_is_listening():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    assert await transcriber_over(handler).liveness() is Liveness.ABSENT
