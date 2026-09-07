import pytest

from fakes import FakeMicLock, FakeRecorder, FakeTranscriber
from speak.capture import Recording, State
from speak.config import Settings
from speak.engines.asr import TranscriptionFailed
from speak.miclock import MicrophoneBusy
from speak.talk import capture

SPOKEN = Recording(wav=b"RIFF", reason=State.DONE)
NOTHING = Recording(wav=b"RIFF", reason=State.SILENT)


async def test_a_spoken_prompt_comes_back_as_text():
    text = await capture(
        Settings(listen=True),
        recorder=FakeRecorder(SPOKEN),
        transcriber=FakeTranscriber(text="run the tests"),
        lock=FakeMicLock(),
    )
    assert text == "run the tests"


async def test_saying_nothing_comes_back_empty():
    text = await capture(
        Settings(listen=True),
        recorder=FakeRecorder(NOTHING),
        transcriber=FakeTranscriber(text="ignored"),
        lock=FakeMicLock(),
    )
    assert text == ""


async def test_a_busy_microphone_is_reported():
    with pytest.raises(MicrophoneBusy):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(),
            lock=FakeMicLock(busy_with=99),
        )


async def test_a_server_without_transcription_is_reported():
    with pytest.raises(TranscriptionFailed, match="speak:setup"):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False),
            lock=FakeMicLock(),
        )


async def test_the_lock_is_released_when_transcription_fails():
    lock = FakeMicLock()
    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(error=TranscriptionFailed("no server")),
            lock=lock,
        )
    assert lock.releases == 1
