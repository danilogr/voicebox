import asyncio
import logging
from typing import NamedTuple

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fakes import FakeEngine, FakeMicLock, FakeQueue, FakeRecorder, FakeTranscriber
from speak import server as server_module
from speak.capture import Recorder, RecorderUnavailable, Recording, State, TrackerSettings
from speak.config import Settings
from speak.engines.asr import TranscriptionFailed
from speak.server import Ears, create_server

SPOKEN = Recording(wav=b"RIFF", reason=State.DONE)
NOTHING = Recording(wav=b"RIFF", reason=State.SILENT)


class Rig(NamedTuple):
    server: object
    queue: FakeQueue
    recorder: Recorder
    transcriber: FakeTranscriber
    lock: FakeMicLock
    calls: list[str]


def build(
    *,
    recording=SPOKEN,
    text="run the tests",
    has_asr=True,
    busy_with=None,
    error=None,
    listen=True,
    language=None,
    limits=None,
    queue=None,
    recorder=None,
    reachable=True,
) -> Rig:
    calls: list[str] = []
    queue = queue if queue is not None else FakeQueue(calls=calls)
    queue.calls = calls
    recorder = recorder if recorder is not None else FakeRecorder(recording, calls=calls)
    recorder.calls = calls
    transcriber = FakeTranscriber(text=text, has_asr=has_asr, error=error, reachable=reachable)
    lock = FakeMicLock(busy_with=busy_with)
    server = create_server(
        settings=Settings(listen=listen, listen_language=language),
        engine=FakeEngine(),
        queue=queue,
        supervisor=None,
        ears=Ears(
            recorder=recorder,
            transcriber=transcriber,
            mic_lock=lock,
            limits=limits if limits is not None else TrackerSettings(),
        ),
    )
    return Rig(server, queue, recorder, transcriber, lock, calls)


async def tool_names(server) -> set[str]:
    return {tool.name for tool in await server.list_tools()}


async def call_listen(server, **arguments: object) -> str:
    result = await server.call_tool("listen", arguments)
    return result.content[0].text


class StuckQueue(FakeQueue):
    """A queue whose worker is dead, so `drain` never returns."""

    async def drain(self) -> None:
        self.calls.append("drain")
        await asyncio.Event().wait()


class BlockingRecorder(FakeRecorder):
    """Holds the microphone open until the test lets it go."""

    def __init__(self, recording: Recording) -> None:
        super().__init__(recording)
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def record(self, settings: TrackerSettings) -> Recording:
        self.started.set()
        await self.finish.wait()
        return await super().record(settings)


async def test_the_tool_is_absent_when_listening_is_off():
    assert "listen" not in await tool_names(build(listen=False).server)


async def test_the_tool_is_absent_without_ears():
    server = create_server(
        settings=Settings(listen=True),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
    )
    assert "listen" not in await tool_names(server)


async def test_the_tool_is_offered_when_voice_input_is_on():
    assert "listen" in await tool_names(build().server)


async def test_the_queue_is_drained_before_the_microphone_opens():
    rig = build()
    await call_listen(rig.server)
    assert rig.calls == ["drain", "record"]


async def test_the_transcript_comes_back():
    assert await call_listen(build(text="run the tests").server) == "run the tests"


async def test_a_recording_with_no_speech_says_so():
    rig = build(recording=NOTHING)
    assert await call_listen(rig.server) == "heard nothing"
    assert rig.transcriber.calls == []


async def test_an_empty_transcript_says_so():
    assert await call_listen(build(text="").server) == "heard nothing"


async def test_a_busy_microphone_names_the_other_session():
    rig = build(busy_with=4242)
    with pytest.raises(ToolError, match="4242"):
        await call_listen(rig.server)
    assert rig.calls == ["drain"]


async def test_a_speech_server_that_is_not_there_is_named_as_such():
    """`has_asr` is False for any HTTP failure, so an absent server needs its own check.

    Reporting "running without a transcription model" would assert the server is up and
    offer two remedies, neither of which applies when nothing is listening on the port.
    """
    rig = build(reachable=False)
    with pytest.raises(ToolError, match="not answering"):
        await call_listen(rig.server)
    assert rig.lock.acquires == 0


async def test_a_server_without_transcription_explains_both_causes():
    rig = build(has_asr=False)
    with pytest.raises(ToolError, match="speak:setup"):
        await call_listen(rig.server)
    assert rig.lock.acquires == 0


async def test_the_lock_is_released_when_transcription_fails():
    rig = build(error=TranscriptionFailed("no server"))
    with pytest.raises(ToolError, match="no server"):
        await call_listen(rig.server)
    assert rig.lock.held is False
    assert rig.lock.releases == 1


async def test_the_lock_is_released_when_the_microphone_fails():
    class BrokenRecorder(FakeRecorder):
        async def record(self, settings: TrackerSettings) -> Recording:
            raise RecorderUnavailable("no microphone binding available")

    rig = build(recorder=BrokenRecorder(SPOKEN))
    with pytest.raises(ToolError, match="no microphone binding"):
        await call_listen(rig.server)
    assert rig.lock.held is False
    assert rig.lock.releases == 1


async def test_the_lock_is_released_after_a_good_recording():
    rig = build()
    await call_listen(rig.server)
    assert rig.lock.held is False
    assert rig.lock.releases == 1


async def test_max_seconds_overrides_the_configured_cap():
    rig = build()
    await call_listen(rig.server, max_seconds=5)
    assert rig.recorder.limits.max_ms == 5000


async def test_the_configured_cap_is_used_without_an_override():
    rig = build(limits=TrackerSettings(max_ms=12_000))
    await call_listen(rig.server)
    assert rig.recorder.limits.max_ms == 12_000


async def test_max_seconds_cannot_raise_the_configured_cap():
    """The configured cap is the machine's limit, not a suggestion the caller may lift."""
    rig = build(limits=TrackerSettings(max_ms=12_000))
    await call_listen(rig.server, max_seconds=600)
    assert rig.recorder.limits.max_ms == 12_000


@pytest.mark.parametrize("nonsense", [0, -5])
async def test_a_non_positive_max_seconds_is_refused(nonsense):
    rig = build()
    with pytest.raises(ToolError, match="greater than 0"):
        await call_listen(rig.server, max_seconds=nonsense)
    assert rig.calls == []


@pytest.mark.parametrize("nonsense", [float("nan"), float("inf")])
async def test_a_non_finite_max_seconds_is_refused(nonsense):
    """`nan` passes every `<= 0` test, so an unbounded recording would hold the mic lock."""
    rig = build()
    with pytest.raises(ToolError, match="finite"):
        await call_listen(rig.server, max_seconds=nonsense)
    assert rig.calls == []


@pytest.mark.parametrize("nonsense", [None, 0.0, -5.0, float("nan"), float("inf"), float("-inf")])
def test_limits_keeps_the_configured_cap_for_anything_that_would_not_bound_a_recording(nonsense):
    """Behind the schema, because a cap that never fires records until the process dies."""
    configured = TrackerSettings(max_ms=12_000)
    assert server_module._limits(configured, nonsense).max_ms == 12_000


async def test_the_configured_language_reaches_the_transcriber():
    rig = build(language="en")
    await call_listen(rig.server)
    assert rig.transcriber.languages == ["en"]


async def test_a_queue_that_will_not_drain_records_anyway(monkeypatch, caplog):
    monkeypatch.setattr(server_module, "DRAIN_TIMEOUT", 0.01)
    rig = build(queue=StuckQueue())
    with caplog.at_level(logging.WARNING, logger="speak.server"):
        assert await call_listen(rig.server) == "run the tests"
    assert rig.calls == ["drain", "record"]
    assert "did not drain" in caplog.text


async def test_a_second_listen_is_refused_while_the_first_is_recording():
    recorder = BlockingRecorder(SPOKEN)
    rig = build(recorder=recorder)
    first = asyncio.create_task(call_listen(rig.server))
    await recorder.started.wait()

    # wait_for so a lost guard fails the test instead of deadlocking the suite.
    with pytest.raises(ToolError, match="already listening"):
        await asyncio.wait_for(call_listen(rig.server), timeout=5.0)
    assert rig.lock.acquires == 1

    recorder.finish.set()
    assert await first == "run the tests"


async def test_listening_can_start_again_after_a_failure():
    rig = build(error=TranscriptionFailed("no server"))
    with pytest.raises(ToolError):
        await call_listen(rig.server)
    rig.transcriber._error = None
    assert await call_listen(rig.server) == "run the tests"
