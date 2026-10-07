"""The first-run notice reaches the user once, on the first reply that can carry it."""

from fakes import FakeEngine, FakeQueue
from voicebox.config import Settings
from voicebox.server import create_server
from voicebox.telemetry.notice import FOR_THE_MODEL, TEXT, Notice


def test_the_notice_is_handed_out_once_and_marked_shown_once():
    marked = []
    notice = Notice(pending=True, mark_shown=lambda: marked.append(1))
    first = notice.take()
    assert first is not None and first.startswith(FOR_THE_MODEL) and TEXT in first
    assert notice.take() is None
    assert marked == [1]


def test_a_notice_already_shown_is_never_handed_out():
    marked = []
    assert Notice(pending=False, mark_shown=lambda: marked.append(1)).take() is None
    assert marked == []


def test_the_text_names_the_runnable_commands_and_never_the_content():
    for command in ("/voicebox:telemetry off", "/voicebox:telemetry local", "/voicebox:stats"):
        assert command in TEXT
    assert "never the text you synthesize" in TEXT


async def test_only_the_first_tool_reply_carries_it():
    served = create_server(
        settings=Settings.from_env({}, platform="darwin"),
        engine=FakeEngine(),
        queue=FakeQueue(),
        supervisor=None,
        notice=Notice(pending=True, mark_shown=lambda: None),
    )
    first = (await served.call_tool("speech_status", {})).content[0].text
    second = (await served.call_tool("speech_status", {})).content[0].text
    assert TEXT in first
    assert TEXT not in second


def test_a_notice_shown_out_of_band_since_startup_is_not_handed_out_again():
    marked = []
    notice = Notice(pending=True, mark_shown=lambda: marked.append(1), still_due=lambda: False)
    assert notice.take() is None
    assert marked == []
