import json
from pathlib import Path

import httpx2

from speak.engines.nemo import NemoEngine
from speak.engines.say import SayEngine
from speak.player import Player

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt "

MODELS = {
    "data": [
        {"id": "whisper", "capability": "transcription"},
        {"id": "magpie", "capability": "speech", "voices": ["John", "Sofia", "Aria"]},
    ]
}


# An explicit player, so these tests assert the same thing on every platform rather
# than whichever binary this machine happens to have.
PLAYER = Player("testplay", ("-q",))


def engine_over(handler, **kwargs: object) -> NemoEngine:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    kwargs.setdefault("player", PLAYER)
    return NemoEngine(base_url="http://127.0.0.1:8082", client=client, **kwargs)


def speech_ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=WAV)


async def test_writes_the_wav_and_hands_it_to_the_resolved_player():
    utterance = await engine_over(speech_ok).prepare("hello", voice=None, rate=None)
    assert utterance.command == "testplay"
    written = Path(utterance.args[-1])
    assert utterance.args[:-1] == ["-q"]
    assert written.read_bytes() == WAV
    assert written.suffix == ".wav"
    await utterance.cleanup()


async def test_a_machine_with_no_player_speaks_through_the_fallback(monkeypatch):
    """Audio that cannot be played is worse than no audio: the sentence would go
    missing silently, where the fallback at least says it."""
    monkeypatch.setattr("speak.engines.nemo.resolve_player", lambda: None)
    engine = engine_over(speech_ok, player=None)
    utterance = await engine.prepare("hello", voice=None, rate=None)
    assert utterance.command == "say"


async def test_cleanup_removes_the_temporary_file():
    utterance = await engine_over(speech_ok).prepare("hello", voice=None, rate=None)
    written = Path(utterance.args[0])
    await utterance.cleanup()
    assert not written.exists()


async def test_cleanup_is_safe_to_run_twice():
    utterance = await engine_over(speech_ok).prepare("hello", voice=None, rate=None)
    await utterance.cleanup()
    await utterance.cleanup()


async def test_sends_the_configured_model_and_the_default_voice():
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, content=WAV)

    engine = engine_over(handler, model="magpie", default_voice="John")
    utterance = await engine.prepare("hello", voice=None, rate=None)
    await utterance.cleanup()

    assert seen[0]["model"] == "magpie"
    assert seen[0]["voice"] == "John"
    assert seen[0]["input"] == "hello"
    assert seen[0]["response_format"] == "wav"


async def test_an_explicit_voice_overrides_the_default():
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, content=WAV)

    utterance = await engine_over(handler, default_voice="John").prepare(
        "hello", voice="Sofia", rate=None
    )
    await utterance.cleanup()
    assert seen[0]["voice"] == "Sofia"


async def test_sends_the_api_key_as_a_bearer_token():
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, content=WAV)

    utterance = await engine_over(handler, api_key="secret").prepare("hello", voice=None, rate=None)
    await utterance.cleanup()
    assert seen == ["Bearer secret"]


async def test_falls_back_to_say_when_the_server_errors():
    notified: list[bool] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    engine = engine_over(
        handler, fallback=SayEngine(), on_unavailable=lambda: notified.append(True)
    )
    utterance = await engine.prepare("hello", voice=None, rate=None)

    assert utterance.command == "say"
    assert utterance.args == ["hello"]
    assert notified == [True]


async def test_falls_back_to_say_when_the_server_is_unreachable():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    engine = engine_over(handler, fallback=SayEngine())
    utterance = await engine.prepare("hello", voice=None, rate=None)
    assert utterance.command == "say"


async def test_the_fallback_keeps_the_rate_but_drops_the_neural_voice():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    engine = engine_over(handler, fallback=SayEngine(), default_voice="John")
    utterance = await engine.prepare("hello", voice=None, rate=190)
    assert utterance.args == ["-r", "190", "hello"]


async def test_lists_the_voices_of_the_speech_capable_model():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=MODELS)

    assert await engine_over(handler).list_voices() == ["John", "Sofia", "Aria"]


async def test_lists_no_voices_when_the_server_is_unreachable():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    assert await engine_over(handler).list_voices() == []


async def test_lists_no_voices_when_no_model_reports_speech():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"id": "whisper", "capability": "x"}]})

    assert await engine_over(handler).list_voices() == []


async def test_trailing_slash_in_the_base_url_does_not_double_up():
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, content=WAV)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    engine = NemoEngine(base_url="http://127.0.0.1:8082/", client=client, player=PLAYER)
    utterance = await engine.prepare("hello", voice=None, rate=None)
    await utterance.cleanup()
    assert seen == ["http://127.0.0.1:8082/v1/audio/speech"]
