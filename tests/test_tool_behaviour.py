import pytest
from mcp.server.mcpserver.exceptions import ToolError
from speak_core.queue import QueueStats

from fakes import FakeEngine, FakeQueue, FakeSupervisor
from voicebox.config import Settings
from voicebox.server import create_server


def build(queue=None, supervisor=None, env=None):
    settings = Settings.from_env(env or {}, platform="darwin")
    queue = queue or FakeQueue()
    server = create_server(
        settings=settings, engine=FakeEngine(), queue=queue, supervisor=supervisor
    )
    return server, queue


async def call(server, name: str, **arguments: object) -> str:
    result = await server.call_tool(name, arguments)
    return result.content[0].text


async def test_read_aloud_collapses_whitespace_and_marks_it_narration():
    server, queue = build()
    await call(server, "read_aloud", text="  hello\n  there  ")
    assert queue.enqueued == [("hello there", None, None)]
    assert queue.intents == ["narration"]


async def test_read_aloud_cuts_at_the_configured_cap():
    server, queue = build(env={"VOICEBOX_MAX_CHARS": "10"})
    await call(server, "read_aloud", text="one two three four five")
    assert queue.enqueued[0][0] == "one two…"


async def test_read_aloud_reads_in_the_configured_voice_and_rate():
    server, queue = build(env={"VOICEBOX_VOICE": "Sofia", "VOICEBOX_RATE": "190"})
    await call(server, "read_aloud", text="hi")
    assert queue.enqueued[0] == ("hi", "Sofia", 190)


@pytest.mark.parametrize(
    ("ahead", "reply"),
    [(0, "reading"), (1, "queued behind 1 sentence"), (3, "queued behind 3 sentences")],
)
async def test_read_aloud_reports_its_place_in_the_queue(ahead, reply):
    server, _ = build(queue=FakeQueue(ahead=ahead))
    assert await call(server, "read_aloud", text="hi") == reply


async def test_empty_text_is_refused_with_a_readable_message():
    server, queue = build()
    with pytest.raises(ToolError, match="nothing to speak"):
        await call(server, "read_aloud", text="   ")
    assert queue.enqueued == []


async def test_reading_registers_with_the_supervisor_once():
    supervisor = FakeSupervisor()
    server, _ = build(supervisor=supervisor)
    await call(server, "read_aloud", text="one")
    await call(server, "read_aloud", text="two")
    assert supervisor.registrations == 1


@pytest.mark.parametrize(
    ("dropped", "reply"), [(0, "stopped; dropped 0 sentences"), (1, "stopped; dropped 1 sentence")]
)
async def test_stop_reading_reports_what_it_dropped(dropped, reply):
    server, queue = build(queue=FakeQueue(dropped=dropped))
    assert await call(server, "stop_reading") == reply
    assert queue.stopped == 1


async def test_reading_status_on_an_idle_queue():
    stats = QueueStats(waiting=0, preparing=0, prepared=0, playing=False, finished=4)
    server, _ = build(queue=FakeQueue(stats=stats))
    assert await call(server, "reading_status") == "nothing left to read; 4 read so far"


async def test_reading_status_counts_what_is_still_to_read():
    stats = QueueStats(waiting=2, preparing=1, prepared=0, playing=True, finished=7)
    server, _ = build(queue=FakeQueue(stats=stats))
    reply = await call(server, "reading_status")
    assert reply.startswith("4 still to read (")
    assert reply.endswith("7 read so far")


async def test_the_narrate_prompt_serves_the_command_with_its_source():
    server, _ = build()
    (prompt,) = await server.list_prompts()
    assert prompt.name == "narrate" and prompt.description
    served = await server.get_prompt("narrate", {"source": "/tmp/notes.md"})
    text = served.messages[0].content.text
    assert not text.startswith("---")
    assert "/tmp/notes.md" in text and "$ARGUMENTS" not in text
    assert "read_aloud" in text and "reading_status" in text and "`listen`" not in text


async def test_concurrent_read_aloud_calls_enqueue_in_arrival_order_despite_a_slow_probe():
    import asyncio

    from voicebox.narration import NarrationTracker

    async def warm() -> bool:
        await asyncio.sleep(0.05)
        return True

    narration = NarrationTracker(
        telemetry=None, engine="nemo", model=None, voice=None, pending=lambda: 0, warm=warm
    )
    queue = FakeQueue()
    server = create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=FakeEngine(),
        queue=queue,
        supervisor=None,
        narration=narration,
    )
    await asyncio.gather(*(call(server, "read_aloud", text=t) for t in ("One.", "Two.", "Three.")))
    assert [text for text, _, _ in queue.enqueued] == ["One.", "Two.", "Three."]
    narration.close()


async def test_a_stop_while_a_read_aloud_waits_on_the_supervisor_leaves_nothing_after_it():
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    class SlowSupervisor(FakeSupervisor):
        async def register(self) -> None:
            entered.set()
            await release.wait()
            await super().register()

    queue = FakeQueue()
    server, _ = build(queue=queue, supervisor=SlowSupervisor())
    reading = asyncio.create_task(call(server, "read_aloud", text="One."))
    await entered.wait()  # read_aloud is suspended inside register, after its enqueue
    assert await call(server, "stop_reading") == "stopped; dropped 0 sentences"
    release.set()
    assert await reading == "reading"
    # The sentence was enqueued before the stop, and the stop came after it.
    assert len(queue.enqueued) == 1 and queue.stopped == 1


async def test_an_unwritable_state_directory_never_fails_a_sentence_already_enqueued(caplog):
    class BrokenSupervisor(FakeSupervisor):
        async def register(self) -> None:
            raise OSError("read-only file system")

    queue = FakeQueue()
    server, _ = build(queue=queue, supervisor=BrokenSupervisor())
    with caplog.at_level("WARNING"):
        assert await call(server, "read_aloud", text="One.") == "reading"
    assert await call(server, "read_aloud", text="Two.") == "reading"
    assert [text for text, _, _ in queue.enqueued] == ["One.", "Two."]
    assert "read-only file system" in caplog.text
