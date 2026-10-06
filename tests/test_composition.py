"""What the composition root decides: who gets a speech server, and with which models."""

from pathlib import Path

from speak.__main__ import build_ears, build_supervisor
from speak.config import ServerSettings, VoiceSettings
from speak.cue import END_SOUND
from speak.listening import transcription_timeout
from speak.settings import Settings


def test_every_session_gets_a_speech_server_because_listening_always_needs_one():
    """Even a `say` session: the microphone transcribes against the shared server."""
    assert build_supervisor(Settings()) is not None
    assert (
        build_supervisor(Settings(speech=VoiceSettings(prefix="SPEAK", engine="nemo"))) is not None
    )


def test_autostart_off_leaves_the_speech_server_to_the_user():
    assert build_supervisor(Settings(server=ServerSettings(autostart=False))) is None
    assert (
        build_supervisor(
            Settings(
                speech=VoiceSettings(prefix="SPEAK", engine="nemo"),
                server=ServerSettings(autostart=False),
            )
        )
        is None
    )


def test_every_session_is_served_the_configured_asr_model():
    """Voice input is unconditional, so the transcription model is always requested.
    A model that will not load is handled at startup, not by withholding it here."""
    supervisor = build_supervisor(Settings(server=ServerSettings(asr_model="parakeet-tdt")))
    assert supervisor is not None
    args = supervisor._server_args()
    assert args[args.index("--asr-model") + 1] == "parakeet-tdt"


def test_there_are_always_ears():
    """`/speak:converse` and `/talk` cannot depend on a switch the user has to find."""
    assert build_ears(Settings()) is not None


def test_the_ears_carry_the_configured_capture_limits():
    ears = build_ears(Settings(listen_max_seconds=5.0, listen_timeout=2.0))
    assert ears is not None
    assert ears.limits.max_ms == 5_000
    assert ears.limits.start_timeout_ms == 2_000


def test_the_transcriber_is_given_a_timeout_the_recording_cap_cannot_outlast():
    """Nothing else sets it, and the default cap is four minutes of audio."""
    settings = Settings(listen_max_seconds=240.0)
    ears = build_ears(settings)
    assert ears is not None
    assert ears.transcriber._timeout == transcription_timeout(settings)


def test_the_ears_carry_a_cue_that_is_on_by_default():
    ears = build_ears(Settings())
    assert ears is not None
    assert ears.cue._enabled is True


def test_the_ears_carry_the_configured_cue_sounds():
    """The defaults are macOS paths, so the override is the only way off it."""
    ears = build_ears(Settings(listen_cue=False, listen_cue_start=Path("/s/in.wav")))
    assert ears is not None
    assert ears.cue._enabled is False
    assert (ears.cue._start, ears.cue._end) == (Path("/s/in.wav"), END_SOUND)


async def test_talk_builds_its_collaborators_the_way_the_server_does(monkeypatch):
    """`/talk` wires up its own objects, and nothing else exercises that.

    The transcriber block is character-identical to the one in `__main__`, which is the
    risk: `tts_model` and `asr_model` are both `str`, so handing the speech model
    to the transcriber would transcribe against a voice and is caught by neither mypy
    nor any other test. Two call sites, so the answer is to pin it rather than extract
    a shared builder.
    """
    import speak.talk as talk

    # `_run` also builds a telemetry session from the real environment; forcing it
    # off keeps this test from touching the machine's actual config directory.
    monkeypatch.setenv("SPEAK_TELEMETRY", "off")

    seen: dict[str, object] = {}

    async def capture_arguments(settings, **collaborators: object) -> str:
        seen.update(collaborators)
        return "a transcript"

    monkeypatch.setattr(talk, "dictate", capture_arguments)
    settings = Settings(
        server=ServerSettings(
            url="http://127.0.0.1:9999", tts_model="magpie", asr_model="parakeet-tdt"
        ),
        listen_max_seconds=30.0,
        state_dir=Path("/tmp/speak-test"),
    )

    assert await talk._run(settings) == "a transcript"

    transcriber = seen["transcriber"]
    assert transcriber._base_url == "http://127.0.0.1:9999"
    assert transcriber._model == "parakeet-tdt"
    assert transcriber._timeout == transcription_timeout(settings)

    supervisor = seen["supervisor"]
    assert supervisor._asr_model == "parakeet-tdt"
    assert supervisor._model == "magpie"
    assert supervisor._port == 9999

    assert seen["lock"]._dir == Path("/tmp/speak-test") / "mic"
    assert seen["cue"]._end == END_SOUND
    assert seen["recorder"]._device is None
