from pathlib import Path

import pytest

from fakes import FakeMicLock, FakePlayer, FakeRecorder, FakeSupervisor, FakeTranscriber
from speak import talk as talk_module
from speak.capture import Recording, State
from speak.config import ServerSettings
from speak.cue import Cue
from speak.engines.asr import TranscriptionFailed
from speak.miclock import MicrophoneBusy
from speak.settings import Settings
from speak.talk import capture, dictate, main
from speak.telemetry import store
from speak.telemetry.session import TelemetrySession
from speak.tools import TALK_HEARD_NOTHING

# Every capture below is given a cue, and none of them makes a sound: the silent one
# never spawns a player, and the rest spawn a fake.
SILENT = Cue(enabled=False)
CUE_START = Path("/sounds/cue-start.aiff")
CUE_END = Path("/sounds/cue-end.aiff")

SPOKEN = Recording(wav=b"RIFF", reason=State.DONE, has_speech=True)
# What a silent room produces when the cap is shorter than the start timeout,
# which is what `listen(max_seconds=8)` builds against the default 10 s timeout.
NOTHING = Recording(wav=b"RIFF", reason=State.CAPPED, has_speech=False)


async def test_a_spoken_prompt_comes_back_as_text():
    text = await capture(
        Settings(),
        recorder=FakeRecorder(SPOKEN),
        transcriber=FakeTranscriber(text="run the tests", reachable=True),
        lock=FakeMicLock(),
        cue=SILENT,
    )
    assert text == "run the tests"


async def test_saying_nothing_comes_back_empty():
    text = await capture(
        Settings(),
        recorder=FakeRecorder(NOTHING),
        transcriber=FakeTranscriber(text="ignored", reachable=True),
        lock=FakeMicLock(),
        cue=SILENT,
    )
    assert text == ""


async def test_a_busy_microphone_is_reported():
    with pytest.raises(MicrophoneBusy):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True),
            lock=FakeMicLock(busy_with=99),
            cue=SILENT,
        )


async def test_a_server_without_transcription_is_reported():
    with pytest.raises(TranscriptionFailed, match="speak:setup"):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=True),
            lock=FakeMicLock(),
            cue=SILENT,
        )


async def test_a_server_that_is_still_loading_says_to_wait():
    """A cold start answers before its models do. Naming a missing model there sends
    the user to /speak:setup or to restart another session; both are wrong, and the
    eager warmup this branch added makes the window the ordinary first run."""
    with pytest.raises(TranscriptionFailed, match="starting up"):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=True, starting=True),
            lock=FakeMicLock(),
            cue=SILENT,
        )


async def test_a_speech_server_that_is_not_there_is_not_called_a_missing_model():
    with pytest.raises(TranscriptionFailed, match="not answering"):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(has_asr=False, reachable=False),
            lock=FakeMicLock(),
            cue=SILENT,
        )


async def test_the_microphone_is_not_opened_while_the_server_is_still_loading():
    lock = FakeMicLock()
    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True, starting=True),
            lock=lock,
            cue=SILENT,
        )
    assert lock.acquires == 0


async def test_the_lock_is_released_when_transcription_fails():
    lock = FakeMicLock()
    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(error=TranscriptionFailed("no server"), reachable=True),
            lock=lock,
            cue=SILENT,
        )
    assert lock.releases == 1


async def test_the_microphone_is_cued_open_and_cued_shut():
    """`/talk` is the acute case: you press enter with no idea whether to talk now or
    in two seconds, and recording stops on silence with nothing to say it has."""
    calls: list[str] = []
    player = FakePlayer(calls)

    await capture(
        Settings(),
        recorder=FakeRecorder(SPOKEN, calls=calls),
        transcriber=FakeTranscriber(text="run the tests", reachable=True),
        lock=FakeMicLock(),
        cue=Cue(start=CUE_START, end=CUE_END, spawn=player.spawn),
    )

    assert calls == ["cue-start", "record", "cue-end"]


async def test_the_cue_waits_for_the_microphone_lock():
    """A cue means the microphone is open, and a refused capture never opens it."""
    player = FakePlayer()

    with pytest.raises(MicrophoneBusy):
        await capture(
            Settings(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True),
            lock=FakeMicLock(busy_with=99),
            cue=Cue(start=CUE_START, end=CUE_END, spawn=player.spawn),
        )

    assert player.commands == []


async def test_a_cue_that_will_not_play_does_not_cost_the_recording():
    player = FakePlayer(error=FileNotFoundError(2, "No such file", "/usr/bin/afplay"))

    text = await capture(
        Settings(),
        recorder=FakeRecorder(SPOKEN),
        transcriber=FakeTranscriber(text="run the tests", reachable=True),
        lock=FakeMicLock(),
        cue=Cue(start=CUE_START, end=CUE_END, spawn=player.spawn),
    )

    assert text == "run the tests"


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
        settings if settings is not None else Settings(),
        supervisor=supervisor,
        recorder=FakeRecorder(SPOKEN),
        transcriber=transcriber,
        lock=lock if lock is not None else FakeMicLock(),
        cue=SILENT,
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
            settings=Settings(server=ServerSettings(autostart=False)),
        )

    assert supervisor.starts == 0
    assert supervisor.releases == 1


async def test_a_speech_server_that_will_not_start_names_the_fix():
    """A missing binary must arrive as one actionable line, not a spawn traceback."""
    supervisor = BrokenSupervisor()

    with pytest.raises(TranscriptionFailed, match="speak:setup"):
        await _dictate(supervisor, FakeTranscriber(reachable=False))

    assert supervisor.releases == 1


def _speaking(monkeypatch, text: str = "", error: Exception | None = None) -> None:
    """Stand in for one recording, so `main` can be driven with no microphone."""

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


# --- telemetry: every /talk attempt leaves exactly one row, however it ended ---


def _telemetry(tmp_path: Path) -> TelemetrySession:
    conn = store.connect(tmp_path / "telemetry.db")
    store.upsert_install(
        conn,
        {
            "install_id": "install-1",
            "first_seen_at": "2026-10-01T10:00:00Z",
            "last_seen_at": "2026-10-01T10:00:00Z",
            "plugin_version": "1.6.0",
            "os": "darwin",
            "arch": "arm64",
            "accel": "metal",
        },
    )
    store.insert_session(
        conn,
        {
            "session_id": "session-1",
            "install_id": "install-1",
            "started_at": "2026-10-01T10:00:00Z",
            "plugin_version": "1.6.0",
            "listen_enabled": True,
            "harness": "claude-code",
        },
    )
    return TelemetrySession(
        conn=conn,
        install_id="install-1",
        session_id="session-1",
        tts_engine="say",
        tts_model=None,
        asr_engine="nemo",
        asr_model="nemotron-3.5",
    )


def _rows(telemetry) -> list[tuple[str, str]]:
    return [(r["source"], r["status"]) for r in store.unshipped_utterances(telemetry.conn)]


class UnwritableMicLock(FakeMicLock):
    def acquire(self) -> None:
        raise PermissionError(13, "Permission denied", "/state/mic.lock")


class UnwritableSupervisor(FakeSupervisor):
    async def register(self) -> None:
        raise PermissionError(13, "Permission denied", "/state/sessions")


def _one_second_wav() -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("supervisor", "transcriber", "lock", "settings", "error"),
    [
        # the server is not there and this process may not start one
        (
            FakeSupervisor(),
            FakeTranscriber(reachable=False),
            FakeMicLock(),
            Settings(server=ServerSettings(autostart=False)),
            TranscriptionFailed,
        ),
        # the server will not start
        (
            BrokenSupervisor(),
            FakeTranscriber(reachable=False),
            FakeMicLock(),
            None,
            TranscriptionFailed,
        ),
        # the state directory cannot be written, at either of the two places it is
        (UnwritableSupervisor(), FakeTranscriber(reachable=True), FakeMicLock(), None, OSError),
        (FakeSupervisor(), FakeTranscriber(reachable=True), UnwritableMicLock(), None, OSError),
    ],
    ids=["autostart-off", "will-not-start", "register-oserror", "lock-oserror"],
)
async def test_a_talk_that_fails_before_recording_writes_one_failed_row(
    tmp_path, supervisor, transcriber, lock, settings, error
):
    telemetry = _telemetry(tmp_path)

    with pytest.raises(error):
        await dictate(
            settings if settings is not None else Settings(),
            supervisor=supervisor,
            recorder=FakeRecorder(SPOKEN),
            transcriber=transcriber,
            lock=lock,
            cue=SILENT,
            telemetry=telemetry,
        )

    assert _rows(telemetry) == [("talk", "failed")]


async def test_a_busy_microphone_writes_one_refused_row(tmp_path):
    telemetry = _telemetry(tmp_path)
    with pytest.raises(MicrophoneBusy):
        await dictate(
            Settings(),
            supervisor=FakeSupervisor(),
            recorder=FakeRecorder(SPOKEN),
            transcriber=FakeTranscriber(reachable=True),
            lock=FakeMicLock(busy_with=99),
            cue=SILENT,
            telemetry=telemetry,
        )
    assert _rows(telemetry) == [("talk", "refused")]


async def test_a_failed_transcription_keeps_the_recordings_length(tmp_path):
    """The audio was captured; only the model failed. Its length is what tells a
    model that fails on long recordings from one that fails on everything."""
    telemetry = _telemetry(tmp_path)
    recording = Recording(wav=_one_second_wav(), reason=State.DONE, has_speech=True)

    with pytest.raises(TranscriptionFailed):
        await capture(
            Settings(),
            recorder=FakeRecorder(recording),
            transcriber=FakeTranscriber(reachable=True, error=TranscriptionFailed("boom")),
            lock=FakeMicLock(),
            cue=SILENT,
            telemetry=telemetry,
        )

    (row,) = store.unshipped_utterances(telemetry.conn)
    assert (row["status"], row["audio_ms"]) == ("failed", 1000)


def test_the_command_file_names_its_host_to_the_launcher(tmp_path):
    """`/talk` is no MCP session, so nothing else tells its session row which host
    it ran under. The launcher exports the name it is given as `SPEAK_HARNESS`."""
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    assert 'scripts/speak-talk" claude-code`' in (root / "commands" / "talk.md").read_text()

    fake_uv = tmp_path / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf %s "$SPEAK_HARNESS"\n')
    fake_uv.chmod(0o755)
    env = {**os.environ, "SPEAK_UV": str(fake_uv)}
    env.pop("SPEAK_HARNESS", None)
    result = subprocess.run(
        ["sh", str(root / "scripts" / "speak-talk"), "claude-code"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "claude-code"
