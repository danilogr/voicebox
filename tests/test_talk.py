from pathlib import Path

import pytest

from fakes import FakeMicLock, FakeRecorder, FakeSupervisor, FakeTranscriber
from speak import talk as talk_module
from speak.capture import Recording, State
from speak.config import Settings
from speak.engines.asr import TranscriptionFailed
from speak.miclock import MicrophoneBusy
from speak.talk import capture, dictate, main
from speak.tools import LISTEN_OFF, TALK_HEARD_NOTHING

SPOKEN = Recording(wav=b"RIFF", reason=State.DONE, has_speech=True)
# What a silent room produces when the cap is shorter than the start timeout,
# which is what `listen(max_seconds=8)` builds against the default 10 s timeout.
NOTHING = Recording(wav=b"RIFF", reason=State.CAPPED, has_speech=False)


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


class BrokenSupervisor(FakeSupervisor):
    """A supervisor whose spawn fails the way a missing `nemo-speech` binary does."""

    async def ensure_running(self) -> bool:
        self.starts += 1
        raise FileNotFoundError(2, "No such file or directory", "/nonexistent")


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch) -> None:
    """`main` reconfigures the `speak` logger; leave the rest of the suite's alone."""
    monkeypatch.setattr(talk_module, "configure_logging", lambda: None)


async def _dictate(supervisor, transcriber, lock=None, settings=None) -> str:
    return await dictate(
        settings if settings is not None else Settings(listen=True),
        supervisor=supervisor,
        recorder=FakeRecorder(SPOKEN),
        transcriber=transcriber,
        lock=lock if lock is not None else FakeMicLock(),
    )


async def test_a_speech_server_that_is_already_up_is_not_started_again():
    """A second server would overwrite the record of the first and orphan it."""
    supervisor = FakeSupervisor()

    text = await _dictate(supervisor, FakeTranscriber(text="run the tests", reachable=True))

    assert text == "run the tests"
    assert supervisor.starts == 0


async def test_a_speech_server_that_is_not_there_is_started():
    supervisor = FakeSupervisor()

    await _dictate(supervisor, FakeTranscriber(text="run the tests", reachable=False))

    assert supervisor.starts == 1


async def test_the_speech_server_is_held_for_this_process_and_given_back():
    """A standalone /talk must not leave a gigabyte of speech server behind it."""
    supervisor = FakeSupervisor()

    await _dictate(supervisor, FakeTranscriber(text="run the tests", reachable=True))

    assert supervisor.registrations == 1
    assert supervisor.releases == 1


async def test_the_speech_server_is_given_back_when_the_recording_fails():
    supervisor = FakeSupervisor()

    with pytest.raises(MicrophoneBusy):
        await _dictate(
            supervisor,
            FakeTranscriber(reachable=True),
            lock=FakeMicLock(busy_with=99),
        )

    assert supervisor.registrations == 1
    assert supervisor.releases == 1


async def test_autostart_off_leaves_the_speech_server_to_the_user():
    """The composition root builds no supervisor under this setting; /talk must agree."""
    supervisor = FakeSupervisor()

    with pytest.raises(TranscriptionFailed, match="not answering"):
        await _dictate(
            supervisor,
            FakeTranscriber(reachable=False),
            settings=Settings(listen=True, nemo_autostart=False),
        )

    assert supervisor.starts == 0
    assert supervisor.releases == 1


async def test_a_speech_server_that_will_not_start_names_the_fix():
    """A missing binary must arrive as one actionable line, not a spawn traceback."""
    supervisor = BrokenSupervisor()

    with pytest.raises(TranscriptionFailed, match="speak:setup"):
        await _dictate(supervisor, FakeTranscriber(reachable=False))

    assert supervisor.releases == 1


def test_voice_input_off_is_refused_before_anything_is_recorded(monkeypatch, capsys):
    monkeypatch.delenv("SPEAK_LISTEN", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == f"speak: {LISTEN_OFF}\n"


def _speaking(monkeypatch, text: str = "", error: Exception | None = None) -> None:
    """Stand in for one recording, so `main` can be driven with no microphone."""
    monkeypatch.setenv("SPEAK_LISTEN", "1")

    async def run(settings) -> str:
        if error is not None:
            raise error
        return text

    monkeypatch.setattr(talk_module, "_run", run)


def test_the_transcript_is_printed_on_stdout(monkeypatch, capsys):
    """stdout is injected into the prompt, so the transcript has to arrive there."""
    _speaking(monkeypatch, text="run the tests")

    main()

    printed = capsys.readouterr()
    assert printed.out == "run the tests\n"
    assert printed.err == ""


def test_silence_prints_the_sentinel_the_command_branches_on(monkeypatch, capsys):
    _speaking(monkeypatch, text="")

    main()

    assert capsys.readouterr().out == f"{TALK_HEARD_NOTHING}\n"


def test_a_failure_is_reported_on_stderr_with_a_non_zero_exit(monkeypatch, capsys):
    _speaking(monkeypatch, error=TranscriptionFailed("could not transcribe: no server"))

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == "speak: could not transcribe: no server\n"


def test_an_os_error_does_not_reach_the_prompt_as_a_traceback(monkeypatch, capsys):
    """Whatever /talk prints is read as the user's own request, tracebacks included."""
    _speaking(monkeypatch, error=FileNotFoundError(2, "No such file or directory", "/nowhere"))

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.count("\n") == 1
    assert "Traceback" not in printed.err


def test_the_command_file_branches_on_the_sentinel_talk_prints():
    """The two are coupled by an exact string; a drift breaks the branch silently."""
    command = Path(__file__).resolve().parent.parent / "commands" / "talk.md"
    assert TALK_HEARD_NOTHING in command.read_text()
