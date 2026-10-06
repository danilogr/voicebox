import asyncio

from fakes import FakeEngine, FakeQueue
from speak import tools
from speak.server import create_server
from speak.settings import Settings
from speak.transcribe.jobs import JobStore
from speak.transcribe.lock import transcription_lock
from speak.transcribe.probe import Probe
from speak.transcribe.service import Scribe

TOOL_NAMES = {"speak", "stop_speaking", "list_voices", "set_voice", "speech_status"}


def server() -> object:
    return create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
    )


async def tools_by_name() -> dict:
    return {tool.name: tool for tool in await server().list_tools()}


async def test_exposes_exactly_the_expected_tools():
    assert set(await tools_by_name()) == TOOL_NAMES


async def test_speak_requires_only_the_text():
    schema = (await tools_by_name())["speak"].input_schema
    assert schema["required"] == ["text"]
    assert set(schema["properties"]) == {"text", "voice", "rate", "intent"}


async def test_speak_intent_is_an_explicit_enum_the_model_can_read():
    """The task list gives the enum in prose (spec 2); the schema has to carry it
    as data too, or a client that only reads the schema never learns the values."""
    schema = (await tools_by_name())["speak"].input_schema
    intent_schema = schema["properties"]["intent"]
    # Optional parameters land as anyOf: [<the type>, {"type": "null"}] with pydantic.
    branches = intent_schema.get("anyOf", [intent_schema])
    enums = [branch["enum"] for branch in branches if "enum" in branch]
    assert len(enums) == 1
    assert set(enums[0]) == set(tools.INTENT_VALUES)


async def test_set_voice_requires_a_voice():
    assert (await tools_by_name())["set_voice"].input_schema["required"] == ["voice"]


async def test_the_listing_tools_take_no_arguments():
    tools = await tools_by_name()
    assert tools["list_voices"].input_schema.get("required", []) == []
    assert tools["stop_speaking"].input_schema.get("required", []) == []


async def test_the_speak_description_carries_the_when_to_speak_policy():
    description = (await tools_by_name())["speak"].description
    assert "finished a task" in description
    assert "blocked" in description
    assert "Do not narrate routine progress" in description


async def test_the_speak_description_asks_for_a_closing_recap():
    description = (await tools_by_name())["speak"].description
    assert "recap" in description
    assert "outcome" in description


async def test_no_description_arrives_with_source_indentation():
    for tool in (await tools_by_name()).values():
        assert tool.description is not None
        indented = [line for line in tool.description.splitlines() if line.startswith("    ")]
        assert indented == [], f"{tool.name} description is indented: {indented}"


async def test_every_tool_is_described():
    for tool in (await tools_by_name()).values():
        assert tool.description and len(tool.description) > 40


async def test_the_transcription_tools_exist_when_a_scribe_is_given(tmp_path):
    from speak.transcribe.service import build_scribe

    settings = Settings.from_env({"SPEAK_STATE_DIR": str(tmp_path)}, platform="darwin")
    served = create_server(
        settings=settings,
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        scribe=build_scribe(settings),
    )
    by_name = {tool.name: tool for tool in await served.list_tools()}
    assert {"transcribe", "transcription_status"} <= set(by_name)
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

    settings = Settings.from_env({"SPEAK_STATE_DIR": str(tmp_path / "state")}, platform="darwin")
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

    settings = Settings.from_env({"SPEAK_STATE_DIR": str(tmp_path / "state")}, platform="darwin")
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
