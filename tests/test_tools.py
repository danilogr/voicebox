import asyncio

from fakes import FakeEngine, FakeQueue
from voicebox import tools
from voicebox.config import Settings
from voicebox.server import create_server
from voicebox.transcribe.jobs import JobStore
from voicebox.transcribe.lock import transcription_lock
from voicebox.transcribe.probe import Probe
from voicebox.transcribe.service import Scribe, build_scribe

SPEECH_TOOLS = {"speak", "stop_speaking", "speech_status"}


def server(scribe=None) -> object:
    return create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        scribe=scribe,
    )


async def tools_by_name(scribe=None) -> dict:
    return {tool.name: tool for tool in await server(scribe).list_tools()}


async def test_without_transcription_only_the_speech_tools_exist():
    assert set(await tools_by_name()) == SPEECH_TOOLS


async def test_speak_requires_only_the_text():
    schema = (await tools_by_name())["speak"].input_schema
    assert schema["required"] == ["text"]


async def test_speak_intent_is_an_explicit_enum():
    branches = (await tools_by_name())["speak"].input_schema["properties"]["intent"]
    enums = [b["enum"] for b in branches.get("anyOf", [branches]) if "enum" in b]
    assert enums and set(enums[0]) == set(tools.INTENT_VALUES)


async def test_no_description_arrives_with_source_indentation():
    for tool in (await tools_by_name()).values():
        assert "\n    " not in tool.description


async def test_every_tool_is_described():
    for tool in (await tools_by_name()).values():
        assert tool.description


async def test_the_transcription_tools_exist_when_a_scribe_is_given(tmp_path):
    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path)}, platform="darwin")
    by_name = await tools_by_name(build_scribe(settings))
    assert set(by_name) == SPEECH_TOOLS | {"transcribe", "transcription_status"}
    assert "instead of Whisper" in by_name["transcribe"].description
    fmt = by_name["transcribe"].input_schema["properties"]["format"]
    assert set(fmt["enum"]) == {"text", "json", "srt", "vtt"}
    assert by_name["transcribe"].input_schema["properties"]["diarize"]["default"] is True


async def test_two_transcribe_calls_never_run_together(tmp_path):
    """The SDK serves tool calls concurrently, and one turn can hold three of them.
    The second must not reach the body while the first is still deciding: the lock it
    would find there is claimed for a worker that has not taken it over yet."""
    entered, release, probed = asyncio.Event(), asyncio.Event(), []
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"x")

    async def probe(path) -> Probe:
        probed.append(path)
        entered.set()
        await release.wait()
        return Probe(60.0, 1024, "wav", "pcm_s16le", 16000, 1)

    async def spawn(argv, log) -> int:
        return 4242

    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path / "state")}, platform="darwin")
    served = create_server(
        settings=settings,
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        scribe=Scribe(
            jobs=JobStore(tmp_path / "state"),
            lock=lambda: transcription_lock(tmp_path / "state", is_alive=lambda pid: True),
            is_alive=lambda pid: True,
            spawn=spawn,
            probe=probe,
        ),
    )

    async def call() -> str:
        result = await served.call_tool("transcribe", {"path": str(source)})
        return result.content[0].text

    first = asyncio.create_task(call())
    await entered.wait()
    second = asyncio.create_task(call())
    await asyncio.sleep(0.05)
    assert probed == [source], "the second call entered the body while the first held it"
    release.set()
    replies = await asyncio.gather(first, second)
    assert sum(reply.startswith("Transcribing") for reply in replies) == 1


async def test_a_second_transcribe_call_is_refused_without_waiting_for_the_first(tmp_path):
    """The gate refuses rather than queues: the second call must come back on its own,
    before the first ever lets go, not merely finish `asyncio.gather` alongside it."""
    entered, release = asyncio.Event(), asyncio.Event()
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"x")

    async def probe(path) -> Probe:
        entered.set()
        await release.wait()
        return Probe(60.0, 1024, "wav", "pcm_s16le", 16000, 1)

    async def spawn(argv, log) -> int:
        return 4242

    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path / "state")}, platform="darwin")
    served = create_server(
        settings=settings,
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        scribe=Scribe(
            jobs=JobStore(tmp_path / "state"),
            lock=lambda: transcription_lock(tmp_path / "state", is_alive=lambda pid: True),
            is_alive=lambda pid: True,
            spawn=spawn,
            probe=probe,
        ),
    )

    async def call() -> str:
        result = await served.call_tool("transcribe", {"path": str(source)})
        return result.content[0].text

    first = asyncio.create_task(call())
    await entered.wait()

    second = await asyncio.wait_for(call(), timeout=1.0)
    assert "already running" in second

    release.set()
    assert (await first).startswith("Transcribing")
