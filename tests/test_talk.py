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
        transcriber=FakeTranscriber(text="run the tests", reachable=True),
        lock=FakeMicLock(),
    )
    assert text == "run the tests"


async def test_saying_nothing_comes_back_empty():
    text = await capture(
        Settings(listen=True),
        recorder=FakeRecorder(NOTHING),
        transcriber=FakeTranscriber(text="ignored", reachable=True),
        lock=FakeMicLock(),
    )
    assert text == ""


async def test_a_busy_microphone_is_reported():
    with pytest.raises(MicrophoneBusy):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True),
            lock=FakeMicLock(busy_with=99),
        )


async def test_a_server_without_transcription_is_reported():
    with pytest.raises(TranscriptionFailed, match="speak:setup"):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=True),
            lock=FakeMicLock(),
        )


async def test_a_server_that_is_still_loading_says_to_wait():
    """A cold start answers before its models do. Naming a missing model there sends
    the user to /speak:setup or to restart another session; both are wrong, and the
    eager warmup this branch added makes the window the ordinary first run."""
    with pytest.raises(TranscriptionFailed, match="starting up"):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=True, starting=True),
            lock=FakeMicLock(),
        )


async def test_a_speech_server_that_is_not_there_is_not_called_a_missing_model():
    with pytest.raises(TranscriptionFailed, match="not answering"):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=False),
            lock=FakeMicLock(),
        )


async def test_the_microphone_is_not_opened_while_the_server_is_still_loading():
    lock = FakeMicLock()
    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True, starting=True),
            lock=lock,
        )
    assert lock.acquires == 0


async def test_the_lock_is_released_when_transcription_fails():
    lock = FakeMicLock()
    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(listen=True),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(error=TranscriptionFailed("no server"), reachable=True),
            lock=lock,
        )
    assert lock.releases == 1


class StartingSupervisor(FakeSupervisor):
    """A supervisor whose start leaves the server answering, the way its probe does."""

    def __init__(self, transcriber: FakeTranscriber) -> None:
        super().__init__()
        self._transcriber = transcriber

    async def ensure_running(self) -> bool:
        await super().ensure_running()
        self._transcriber.becomes_ready()
        return True


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


async def test_a_speech_server_that_is_not_there_is_started_and_then_used():
    """`ensure_running` does not return until the server answers, so the capture that
    follows must go through rather than report the server it just started missing."""
    transcriber = FakeTranscriber(text="run the tests", reachable=False)
    supervisor = StartingSupervisor(transcriber)

    assert await _dictate(supervisor, transcriber) == "run the tests"
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
    assert printed.out == f"speak: {LISTEN_OFF}\n"
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


def test_a_failure_reaches_the_prompt_as_well_as_the_log(monkeypatch, capsys):
    """The command file branches on a line beginning `speak:`, so the line has to be
    in the message. Only stdout is certainly injected; stderr from a non-zero exit is
    not this plugin's to guarantee, and its absence would leave an empty transcript
    under an instruction to act on it as the user's request."""
    _speaking(monkeypatch, error=TranscriptionFailed("could not transcribe: no server"))

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == "speak: could not transcribe: no server\n"
    assert printed.err == "speak: could not transcribe: no server\n"


def test_an_error_on_stdout_cannot_be_mistaken_for_the_user_talking(monkeypatch, capsys):
    _speaking(monkeypatch, error=TranscriptionFailed("no server"))

    with pytest.raises(SystemExit):
        main()

    assert capsys.readouterr().out.startswith("speak: ")


def test_an_os_error_does_not_reach_the_prompt_as_a_traceback(monkeypatch, capsys):
    """Whatever /talk prints is read as the user's own request, tracebacks included."""
    _speaking(monkeypatch, error=FileNotFoundError(2, "No such file or directory", "/nowhere"))

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    printed = capsys.readouterr()
    assert printed.out.count("\n") == 1
    assert printed.err.count("\n") == 1
    assert "Traceback" not in printed.out + printed.err


def test_the_command_file_branches_on_the_sentinel_talk_prints():
    """The two are coupled by an exact string; a drift breaks the branch silently."""
    command = Path(__file__).resolve().parent.parent / "commands" / "talk.md"
    assert TALK_HEARD_NOTHING in command.read_text()
