import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fakes import FakeEngine, FakeQueue, FakeSupervisor
from speak.queue import QueueStats
from speak.server import create_server
from speak.settings import Settings


def build(engine=None, queue=None, supervisor=None, env=None):
    settings = Settings.from_env(env or {}, platform="darwin")
    engine = engine or FakeEngine()
    queue = queue or FakeQueue()
    server = create_server(settings=settings, engine=engine, queue=queue, supervisor=supervisor)
    return server, engine, queue


async def call(server, name: str, **arguments: object) -> str:
    result = await server.call_tool(name, arguments)
    return result.content[0].text


async def test_speak_collapses_whitespace_before_queueing():
    server, _, queue = build()
    await call(server, "speak", text="  hello\n  there  ")
    assert queue.enqueued[0][0] == "hello there"


async def test_speak_truncates_at_the_configured_cap():
    server, _, queue = build(env={"SPEAK_MAX_CHARS": "11"})
    await call(server, "speak", text="one two three four")
    assert queue.enqueued[0][0] == "one two…"


async def test_speak_reports_that_it_is_speaking_when_nothing_is_ahead():
    server, _, _ = build(queue=FakeQueue(ahead=0))
    assert await call(server, "speak", text="hi") == 'speaking: "hi"'


async def test_speak_reports_the_queue_position_with_singular_wording():
    server, _, _ = build(queue=FakeQueue(ahead=1))
    assert await call(server, "speak", text="hi") == 'queued behind 1 utterance: "hi"'


async def test_speak_reports_the_queue_position_with_plural_wording():
    server, _, _ = build(queue=FakeQueue(ahead=3))
    assert await call(server, "speak", text="hi") == 'queued behind 3 utterances: "hi"'


async def test_speak_rejects_empty_text_with_a_message_the_model_can_read():
    server, _, queue = build()
    with pytest.raises(ToolError, match="nothing to speak"):
        await call(server, "speak", text="   ")
    assert queue.enqueued == []


async def test_speak_uses_the_starting_voice_from_the_environment():
    server, _, queue = build(env={"SPEAK_VOICE": "Samantha", "SPEAK_RATE": "190"})
    await call(server, "speak", text="hi")
    assert queue.enqueued[0] == ("hi", "Samantha", 190)


async def test_an_explicit_voice_overrides_the_session_voice():
    server, _, queue = build(env={"SPEAK_VOICE": "Samantha"})
    await call(server, "speak", text="hi", voice="Sofia")
    assert queue.enqueued[0][1] == "Sofia"


async def test_speak_registers_with_the_supervisor_exactly_once():
    supervisor = FakeSupervisor()
    server, _, _ = build(supervisor=supervisor)
    await call(server, "speak", text="one")
    await call(server, "speak", text="two")
    assert supervisor.registrations == 1


async def test_speak_works_without_a_supervisor():
    server, _, queue = build(supervisor=None)
    await call(server, "speak", text="hi")
    assert len(queue.enqueued) == 1


async def test_list_voices_names_the_engine_and_the_available_voices():
    server, _, _ = build(engine=FakeEngine(voices=["John", "Sofia"], default_voice="John"))
    message = await call(server, "list_voices")
    assert "engine: fake" in message
    assert "active voice: John" in message
    assert "Available: John, Sofia." in message


async def test_list_voices_says_so_when_the_engine_reports_none():
    server, _, _ = build(engine=FakeEngine(voices=[]))
    message = await call(server, "list_voices")
    assert "does not report voice names" in message
    assert "active voice: the engine default" in message


async def test_set_voice_matches_case_insensitively_and_applies_to_later_speech():
    server, _, queue = build(engine=FakeEngine(voices=["Sofia"]))
    assert await call(server, "set_voice", voice="sofia") == "voice set to Sofia for this session"
    await call(server, "speak", text="hi")
    assert queue.enqueued[0][1] == "Sofia"


async def test_set_voice_rejects_a_name_the_engine_does_not_have():
    server, _, _ = build(engine=FakeEngine(voices=["John", "Sofia"]))
    with pytest.raises(ToolError, match="unknown voice"):
        await call(server, "set_voice", voice="Bruce")


async def test_set_voice_lists_the_real_names_when_it_rejects():
    server, _, _ = build(engine=FakeEngine(voices=["John", "Sofia"]))
    with pytest.raises(ToolError, match="John, Sofia"):
        await call(server, "set_voice", voice="Bruce")


async def test_set_voice_accepts_any_name_when_the_engine_lists_none():
    server, _, _ = build(engine=FakeEngine(voices=[]))
    assert "Bruce" in await call(server, "set_voice", voice="Bruce")


async def test_set_voice_rejects_blank_input():
    server, _, _ = build()
    with pytest.raises(ToolError, match="needs a voice name"):
        await call(server, "set_voice", voice="   ")


async def test_stop_speaking_reports_how_many_were_dropped():
    server, _, queue = build(queue=FakeQueue(dropped=3))
    assert await call(server, "stop_speaking") == "stopped; dropped 3 utterances"
    assert queue.stopped == 1


async def test_stop_speaking_uses_singular_wording_for_one():
    server, _, _ = build(queue=FakeQueue(dropped=1))
    assert await call(server, "stop_speaking") == "stopped; dropped 1 utterance"


async def test_stop_speaking_on_silence_reports_zero():
    server, _, _ = build(queue=FakeQueue(dropped=0))
    assert await call(server, "stop_speaking") == "stopped; dropped 0 utterances"


async def test_speech_status_reports_an_idle_queue():
    server, _, _ = build()
    assert "nothing queued" in await call(server, "speech_status")


async def test_speech_status_reports_what_is_in_hand():
    """A slow machine is indistinguishable from a stuck one without this: a caller that
    can see three waiting and one playing knows to wait rather than to give up."""
    busy = FakeQueue(stats=QueueStats(waiting=3, preparing=1, prepared=1, playing=True, finished=7))
    server, _, _ = build(queue=busy)
    said = await call(server, "speech_status")
    assert "playing" in said
    assert "3 waiting" in said
    assert "1 prepared" in said
    assert "7" in said


async def test_speech_status_names_the_one_utterance_playing():
    """The commonest non-idle state: one utterance playing with nothing behind it."""
    busy = FakeQueue(stats=QueueStats(waiting=0, preparing=0, prepared=0, playing=True, finished=2))
    server, _, _ = build(queue=busy)
    assert await call(server, "speech_status") == "one playing; 2 spoken so far"
