import asyncio
import logging
from pathlib import Path
from typing import NamedTuple

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from speak_core.engines.asr import TranscriptionFailed
from speak_core.queue import QueueStats

from fakes import FakeEngine, FakeMicLock, FakePlayer, FakeQueue, FakeRecorder, FakeTranscriber
from speak import server as server_module
from speak.capture import Recorder, RecorderUnavailable, Recording, State, TrackerSettings
from speak.cue import Cue
from speak.server import Ears, create_server
from speak.settings import Settings

# Named for their stems, which is what the fake player logs when a cue finishes.
CUE_START = Path("/sounds/cue-start.aiff")
CUE_END = Path("/sounds/cue-end.aiff")
CUE_EMPTY = Path("/sounds/cue-empty.aiff")

SPOKEN = Recording(wav=b"RIFF", reason=State.DONE, has_speech=True)
# Capped rather than silent: a recording of an empty room ends CAPPED whenever
# the cap is the shorter limit, and it still must not reach the transcriber.
NOTHING = Recording(wav=b"RIFF", reason=State.CAPPED, has_speech=False)


class Rig(NamedTuple):
    server: object
    queue: FakeQueue
    recorder: Recorder
    transcriber: FakeTranscriber
    lock: FakeMicLock
    player: FakePlayer
    calls: list[str]


def build(
    *,
    recording=SPOKEN,
    text="run the tests",
    has_asr=True,
    busy_with=None,
    error=None,
    language=None,
    limits=None,
    queue=None,
    recorder=None,
    reachable=True,
    starting=False,
    player=None,
    cue=None,
) -> Rig:
    calls: list[str] = []
    queue = queue if queue is not None else FakeQueue(calls=calls)
    queue.calls = calls
    recorder = recorder if recorder is not None else FakeRecorder(recording, calls=calls)
    recorder.calls = calls
    player = player if player is not None else FakePlayer()
    player.calls = calls
    transcriber = FakeTranscriber(
        text=text, has_asr=has_asr, error=error, reachable=reachable, starting=starting
    )
    lock = FakeMicLock(busy_with=busy_with)
    server = create_server(
        settings=Settings(listen_language=language),
        engine=FakeEngine(),
        queue=queue,
        supervisor=None,
        ears=Ears(
            recorder=recorder,
            transcriber=transcriber,
            mic_lock=lock,
            limits=limits if limits is not None else TrackerSettings(),
            cue=cue
            if cue is not None
            else Cue(start=CUE_START, end=CUE_END, empty=CUE_EMPTY, spawn=player.spawn),
        ),
    )
    return Rig(server, queue, recorder, transcriber, lock, player, calls)


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


class BrokenRecorder(FakeRecorder):
    """A microphone that fails the way a missing binding does."""

    async def record(self, settings: TrackerSettings) -> Recording:
        raise RecorderUnavailable("no microphone binding available")


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


async def test_the_tool_is_always_there():
    """There is no switch left to lose it to."""
    assert "listen" in await tool_names(build().server)


async def test_the_tool_is_absent_without_ears():
    server = create_server(
        settings=Settings(),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
    )
    assert "listen" not in await tool_names(server)


async def test_the_tool_is_offered_when_voice_input_is_on():
    assert "listen" in await tool_names(build().server)


async def test_the_queue_is_drained_before_the_microphone_opens():
    """And the opening cue is over before the recorder is called: the noise gate is
    calibrated from the first 300 ms of audio, so a chime still sounding then is
    averaged into it and the recording ends in silence."""
    rig = build()
    await call_listen(rig.server)
    assert rig.calls == ["drain", "cue-start", "record", "cue-end"]


async def test_the_closing_cue_sounds_before_the_transcript_is_asked_for():
    """It says the microphone shut, which happens when recording stops rather than
    seconds later when the words come back."""
    rig = build()
    rig.transcriber.calls = rig.calls
    await call_listen(rig.server)
    assert rig.calls == ["drain", "cue-start", "record", "cue-end", "transcribe"]


async def test_a_recording_that_fails_closes_with_the_error_sound():
    """The bracket still closes, and it closes with the sound that says nothing was
    captured rather than the one that says the utterance was taken."""
    rig = build(recorder=BrokenRecorder(SPOKEN))
    with pytest.raises(ToolError, match="no microphone binding"):
        await call_listen(rig.server)
    assert rig.calls == ["drain", "cue-start", "cue-empty"]


async def test_a_listen_that_is_refused_makes_no_sound():
    """A cue means the microphone is open, and a refused call never opens it."""
    busy = build(busy_with=4242)
    with pytest.raises(ToolError):
        await call_listen(busy.server)

    absent = build(reachable=False)
    with pytest.raises(ToolError):
        await call_listen(absent.server)

    assert busy.player.commands == []
    assert absent.player.commands == []


async def test_a_cue_that_will_not_play_does_not_cost_the_recording():
    """A cue is a convenience; a recording is not."""
    player = FakePlayer(error=FileNotFoundError(2, "No such file", "/usr/bin/afplay"))
    rig = build(player=player)
    assert await call_listen(rig.server) == "run the tests"
    assert rig.calls == ["drain", "record"]


async def test_cues_can_be_turned_off():
    rig = build(cue=Cue(enabled=False))
    assert await call_listen(rig.server) == "run the tests"
    assert rig.calls == ["drain", "record"]
    assert rig.player.commands == []


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


async def test_listen_waits_for_this_sessions_own_warmup(monkeypatch):
    """The warmup runs unawaited, so the first `listen` of a session can arrive while
    the server that session just spawned is still loading. Judging it then names
    remedies for a condition that is about to clear on its own."""
    waited: list[str] = []

    async def settle(warmup) -> None:
        waited.append("settle")

    monkeypatch.setattr(server_module, "settle", settle)
    rig = build()

    await call_listen(rig.server)

    assert waited == ["settle"]


async def test_a_speech_server_that_is_not_there_is_named_as_such():
    """`has_asr` is False for any HTTP failure, so an absent server needs its own check.

    Reporting "running without a transcription model" would assert the server is up and
    offer two remedies, neither of which applies when nothing is listening on the port.
    """
    rig = build(reachable=False)
    with pytest.raises(ToolError, match="not answering"):
        await call_listen(rig.server)
    assert rig.lock.acquires == 0


async def test_a_server_that_is_still_loading_says_to_wait():
    """The server starts as the session opens, so the first `listen`
    can arrive while it is still loading. "No transcription model" would be wrong."""
    rig = build(reachable=True, starting=True, has_asr=False)
    with pytest.raises(ToolError, match="starting up"):
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


async def test_a_wedged_queue_is_refused_rather_than_recorded_over(monkeypatch, caplog):
    """Recording while this session is still talking hands our own voice back where the
    user's answer belongs, and the model acts on it as a request. A refusal can be
    retried; a fabricated answer cannot be taken back. Nothing is cued or recorded."""
    monkeypatch.setattr(server_module, "DRAIN_BACKSTOP", 0.01)
    rig = build(queue=StuckQueue())
    with (
        caplog.at_level(logging.WARNING, logger="speak.server"),
        pytest.raises(ToolError, match="stop_speaking"),
    ):
        await call_listen(rig.server)
    assert rig.calls == ["drain"]
    assert "wedged" in caplog.text


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


# --- conversation mode, served to every MCP client ---


async def prompt_names(server) -> set[str]:
    return {prompt.name for prompt in await server.list_prompts()}


async def test_conversation_mode_is_offered_to_every_client_when_voice_input_is_on():
    """Claude Code reads commands/*.md itself; any other MCP client sees only what the
    server offers, so without the prompt conversation mode exists in one app."""
    assert "converse" in await prompt_names(build().server)


async def test_the_prompt_is_the_command_file_without_its_frontmatter():
    """One copy of the instruction and of its description, whichever app asks."""
    description, body = server_module.split_frontmatter(
        server_module.CONVERSE_COMMAND.read_text(encoding="utf-8")
    )
    server = build().server

    result = await server.get_prompt("converse")
    served = result.messages[0].content.text
    assert served == body
    assert not served.startswith("---")
    assert "listen" in served

    listed = {prompt.name: prompt.description for prompt in await server.list_prompts()}
    assert description
    assert listed["converse"] == description


async def test_a_missing_command_file_costs_the_prompt_and_nothing_else(monkeypatch, tmp_path):
    """A checkout without the file must still speak and listen."""
    monkeypatch.setattr(server_module, "CONVERSE_COMMAND", tmp_path / "gone.md")
    rig = build()
    assert "converse" not in await prompt_names(rig.server)
    assert "listen" in await tool_names(rig.server)


def test_frontmatter_is_split_into_its_description_and_the_body():
    text = "---\ndescription: Talk: out loud\nallowed-tools: Bash\n---\n\nThe body.\n"
    assert server_module.split_frontmatter(text) == ("Talk: out loud", "The body.\n")


def test_a_file_without_frontmatter_is_all_body():
    assert server_module.split_frontmatter("Just text.\n") == ("", "Just text.\n")


def test_an_unclosed_frontmatter_block_is_left_in_the_body():
    """Stripping to the end of the file would serve an empty instruction."""
    text = "---\ndescription: never closed\nThe body.\n"
    assert server_module.split_frontmatter(text) == ("", text)


async def test_a_refusal_says_how_much_is_still_to_play(monkeypatch):
    monkeypatch.setattr(server_module, "DRAIN_BACKSTOP", 0.01)
    rig = build(
        queue=StuckQueue(
            stats=QueueStats(waiting=2, preparing=1, prepared=0, playing=True, finished=4)
        )
    )
    with pytest.raises(ToolError, match="2 waiting"):
        await call_listen(rig.server)


async def test_a_refusal_with_nothing_in_hand_says_so(monkeypatch):
    """All-zero stats can mean the queue went quiet between the timeout firing and the
    stats() read, or a worker that died holding nothing - the message reports what was
    observed, not which. It must not read as both wedged and empty of anything pending."""
    monkeypatch.setattr(server_module, "DRAIN_BACKSTOP", 0.01)
    rig = build(
        queue=StuckQueue(
            stats=QueueStats(waiting=0, preparing=0, prepared=0, playing=False, finished=4)
        )
    )
    with pytest.raises(ToolError, match="nothing in hand") as excinfo:
        await call_listen(rig.server)
    assert "0 waiting" not in str(excinfo.value)


async def test_a_refusal_names_the_prepared_utterance(monkeypatch):
    """Audio ready but playback never started points at a dead player subprocess, not a
    slow TTS backend - the two need `stats.prepared` to tell apart."""
    monkeypatch.setattr(server_module, "DRAIN_BACKSTOP", 0.01)
    rig = build(
        queue=StuckQueue(
            stats=QueueStats(waiting=0, preparing=0, prepared=1, playing=False, finished=4)
        )
    )
    with pytest.raises(ToolError, match="1 prepared"):
        await call_listen(rig.server)
