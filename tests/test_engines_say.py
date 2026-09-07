from speak.engines.say import SayEngine, parse_voice_listing

LISTING = """Albert              en_US    # Hello! My name is Albert.
Alice               it_IT    # Ciao! Mi chiamo Alice.
Eddy (English (UK)) en_GB    # Hello! My name is Eddy.
Eddy (German (Germany)) de_DE    # Hallo! Ich heisse Eddy.
Samantha            en_US    # Hello! My name is Samantha.

"""


def test_parses_plain_names():
    assert "Albert" in parse_voice_listing(LISTING, "en")


def test_keeps_names_containing_spaces_and_nested_parentheses():
    assert "Eddy (English (UK))" in parse_voice_listing(LISTING, "en")


def test_filters_by_language():
    voices = parse_voice_listing(LISTING, "en")
    assert "Alice" not in voices
    assert "Eddy (German (Germany))" not in voices


def test_no_language_filter_keeps_every_voice():
    assert len(parse_voice_listing(LISTING, None)) == 5


def test_ignores_blank_lines():
    assert "" not in parse_voice_listing(LISTING, None)


async def test_prepare_puts_text_last_as_its_own_argument():
    utterance = await SayEngine().prepare("hello there", voice=None, rate=None)
    assert utterance.command == "say"
    assert utterance.args == ["hello there"]


async def test_prepare_passes_voice_and_rate_as_flags():
    utterance = await SayEngine().prepare("hi", voice="Samantha", rate=200)
    assert utterance.args == ["-v", "Samantha", "-r", "200", "hi"]


async def test_prepare_omits_rate_when_not_set():
    utterance = await SayEngine().prepare("hi", voice="Samantha", rate=None)
    assert utterance.args == ["-v", "Samantha", "hi"]


async def test_list_voices_uses_the_injected_listing():
    engine = SayEngine(run_listing=_fixed_listing, language="en")
    assert await engine.list_voices() == ["Albert", "Eddy (English (UK))", "Samantha"]


async def test_list_voices_returns_empty_when_say_is_unavailable():
    async def explode() -> str:
        raise OSError("say not found")

    assert await SayEngine(run_listing=explode).list_voices() == []


async def _fixed_listing() -> str:
    return LISTING
