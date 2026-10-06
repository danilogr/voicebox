import pytest

from speak.engines.command import CommandEngine


async def test_appends_the_text_as_a_single_final_argument():
    utterance = await CommandEngine("piper --model en_US.onnx").prepare(
        "two words", voice=None, rate=None
    )
    assert utterance.command == "piper"
    assert utterance.args == ["--model", "en_US.onnx", "two words"]


async def test_reports_no_voices():
    assert await CommandEngine("piper").list_voices() == []


@pytest.mark.parametrize("command", ["", "   "])
def test_rejects_an_empty_command_at_construction(command):
    with pytest.raises(ValueError, match="needs a command line"):
        CommandEngine(command)
