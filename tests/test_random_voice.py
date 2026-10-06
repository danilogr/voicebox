"""A session can open on a voice chosen at random."""

import random

from fakes import FakeEngine, FakeQueue
from speak.server import choose_pool, create_server
from speak.settings import Settings

SAY_LIKE = ["Samantha", "Karen", "Zarvox", "Bubbles", "Albert"]
CURATED = ("Samantha", "Karen", "Moira")


def build(engine=None, queue=None, env=None, seed=0):
    return create_server(
        settings=Settings.from_env(env or {}, platform="darwin"),
        engine=engine or FakeEngine(),
        queue=queue or FakeQueue(),
        supervisor=None,
        rng=random.Random(seed),
    )


async def call(server, name: str, **arguments: object) -> str:
    result = await server.call_tool(name, arguments)
    return result.content[0].text


# choose_pool is the whole policy, so it is tested directly.


def test_an_explicit_pool_wins_over_everything():
    assert choose_pool(("A", "B"), CURATED, SAY_LIKE) == ["A", "B"]


def test_an_explicit_pool_works_even_when_the_engine_reports_nothing():
    assert choose_pool(("A", "B"), None, []) == ["A", "B"]


def test_the_engine_pool_is_intersected_with_what_is_installed():
    assert choose_pool((), CURATED, SAY_LIKE) == ["Samantha", "Karen"]


def test_an_engine_pool_that_matches_nothing_falls_back_to_every_voice():
    assert choose_pool((), ("Nonexistent",), SAY_LIKE) == SAY_LIKE


def test_without_an_engine_pool_every_reported_voice_is_eligible():
    assert choose_pool((), None, SAY_LIKE) == SAY_LIKE


def test_nothing_reported_and_no_pool_means_no_choice():
    assert choose_pool((), None, []) == []


# End-to-end through the tools.


async def test_random_never_reaches_the_engine_as_a_literal_voice_name():
    queue = FakeQueue()
    server = build(engine=FakeEngine(voices=SAY_LIKE), queue=queue, env={"SPEAK_VOICE": "random"})
    await call(server, "speak", text="hi")
    assert queue.enqueued[0][1] != "random"


async def test_a_random_session_voice_comes_from_the_engine_pool():
    queue = FakeQueue()
    engine = FakeEngine(voices=SAY_LIKE, random_pool=CURATED)
    server = build(engine=engine, queue=queue, env={"SPEAK_VOICE": "random"})
    await call(server, "speak", text="hi")
    assert queue.enqueued[0][1] in ("Samantha", "Karen")


async def test_a_configured_pool_overrides_the_engine_pool():
    queue = FakeQueue()
    engine = FakeEngine(voices=SAY_LIKE, random_pool=CURATED)
    server = build(
        engine=engine,
        queue=queue,
        env={"SPEAK_VOICE": "random", "SPEAK_VOICE_POOL": "Zarvox,Bubbles"},
    )
    await call(server, "speak", text="hi")
    assert queue.enqueued[0][1] in ("Zarvox", "Bubbles")


async def test_the_chosen_voice_sticks_for_the_whole_session():
    queue = FakeQueue()
    server = build(engine=FakeEngine(voices=SAY_LIKE), queue=queue, env={"SPEAK_VOICE": "random"})
    await call(server, "speak", text="one")
    await call(server, "speak", text="two")
    await call(server, "speak", text="three")
    chosen = {entry[1] for entry in queue.enqueued}
    assert len(chosen) == 1


async def test_a_cold_engine_speaks_with_the_default_and_resolves_later():
    """nemo reports no voices until its server is up; the sentence still goes out."""
    queue = FakeQueue()
    engine = FakeEngine(voices=[])
    server = build(engine=engine, queue=queue, env={"SPEAK_VOICE": "random"})

    await call(server, "speak", text="cold")
    assert queue.enqueued[0][1] is None

    engine._voices = ["John", "Sofia", "Aria"]
    await call(server, "speak", text="warm")
    assert queue.enqueued[1][1] in ("John", "Sofia", "Aria")


async def test_set_voice_overrides_a_random_pick():
    queue = FakeQueue()
    server = build(engine=FakeEngine(voices=SAY_LIKE), queue=queue, env={"SPEAK_VOICE": "random"})
    await call(server, "speak", text="one")
    await call(server, "set_voice", voice="Karen")
    await call(server, "speak", text="two")
    assert queue.enqueued[1][1] == "Karen"


async def test_list_voices_reports_the_voice_that_was_rolled():
    engine = FakeEngine(voices=SAY_LIKE, random_pool=CURATED)
    server = build(engine=engine, env={"SPEAK_VOICE": "random"})
    message = await call(server, "list_voices")
    assert "active voice: Samantha" in message or "active voice: Karen" in message


async def test_a_pinned_voice_is_still_pinned():
    queue = FakeQueue()
    server = build(engine=FakeEngine(voices=SAY_LIKE), queue=queue, env={"SPEAK_VOICE": "Samantha"})
    await call(server, "speak", text="hi")
    assert queue.enqueued[0][1] == "Samantha"


async def test_an_explicit_voice_argument_still_wins_over_the_random_pick():
    queue = FakeQueue()
    server = build(engine=FakeEngine(voices=SAY_LIKE), queue=queue, env={"SPEAK_VOICE": "random"})
    await call(server, "speak", text="hi", voice="Zarvox")
    assert queue.enqueued[0][1] == "Zarvox"
