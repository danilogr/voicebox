import asyncio
from typing import Never

from fakes import FakeEngine, FakeQueue, installed
from voicebox.config import Settings
from voicebox.jobs.desk import Desk, build_desk
from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import JobStore
from voicebox.server import create_server
from voicebox.transcribe.probe import Probe

SPEECH_TOOLS = {"read_aloud", "reading_status", "stop_reading"}


def server(desk=None) -> object:
    return create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        desk=desk,
    )


async def tools_by_name(desk=None) -> dict:
    return {tool.name: tool for tool in await server(desk).list_tools()}


async def test_without_transcription_only_the_speech_tools_exist():
    assert set(await tools_by_name()) == SPEECH_TOOLS


async def test_read_aloud_requires_only_the_text():
    schema = (await tools_by_name())["read_aloud"].input_schema
    assert schema["required"] == ["text"] and set(schema["properties"]) == {"text"}


async def test_no_description_arrives_with_source_indentation():
    for tool in (await tools_by_name()).values():
        assert "\n    " not in tool.description


async def test_every_tool_is_described():
    for tool in (await tools_by_name()).values():
        assert tool.description


async def test_the_transcription_tools_exist_when_a_desk_is_given(tmp_path):
    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path)}, platform="darwin")
    by_name = await tools_by_name(build_desk(settings))
    assert set(by_name) == SPEECH_TOOLS | {"transcribe", "job_status"}
    assert "instead of Whisper" in by_name["transcribe"].description
    fmt = by_name["transcribe"].input_schema["properties"]["format"]
    assert set(fmt["enum"]) == {"text", "json", "srt", "vtt"}
    assert by_name["transcribe"].input_schema["properties"]["diarize"]["default"] is True


async def test_two_transcribe_calls_never_run_together(tmp_path):
    """The SDK serves tool calls concurrently, and one turn can hold three of them.
    The second must not reach the body while the first is still deciding, or both
    could pass the check that their outputs differ. It waits, then joins the queue."""
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
        desk=Desk(
            jobs=JobStore(tmp_path / "state"),
            lock=lambda: job_lock(tmp_path / "state", is_alive=lambda pid: True),
            is_alive=lambda pid: True,
            spawn=spawn,
            probe=probe,
            which=installed,
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
    assert all(reply.startswith("Transcribing") for reply in replies)
    assert sum("queued behind 1 job" in reply for reply in replies) == 1


async def test_job_status_answers_for_an_empty_store(tmp_path):
    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path)}, platform="darwin")
    served = server(build_desk(settings))
    result = await served.call_tool("job_status", {})
    assert result.content[0].text == "no jobs yet"


async def test_synthesize_exists_with_a_speech_server_and_takes_its_formats(tmp_path):
    from voicebox.synthesize.service import Speech

    async def liveness() -> Never:
        raise AssertionError("not called while listing tools")

    async def voices() -> list[str]:
        return []

    settings = Settings.from_env({"VOICEBOX_STATE_DIR": str(tmp_path)}, platform="darwin")
    served = create_server(
        settings=settings,
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        desk=build_desk(settings),
        speech=Speech(liveness=liveness, voices=voices),
    )
    by_name = {tool.name: tool for tool in await served.list_tools()}
    assert set(by_name) == SPEECH_TOOLS | {"transcribe", "job_status", "synthesize"}
    schema = by_name["synthesize"].input_schema
    assert schema.get("required", []) == []
    assert set(schema["properties"]["format"]["enum"]) == {"wav", "m4a", "mp3"}
    assert schema["properties"]["format"]["default"] == "wav"
    assert "never overwritten" in by_name["synthesize"].description
