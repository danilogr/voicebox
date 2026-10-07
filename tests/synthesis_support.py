"""Fakes for the synthesis tests: a speech server that answers as told, a supervisor
that counts, and a staged job ready for its worker."""

import io
import wave
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Never

from speak_core.config import ServerSettings
from speak_core.engines.asr import Liveness
from speak_core.engines.nemo import SynthesisFailed

from voicebox.config import Settings
from voicebox.jobs.desk import Desk
from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import RUNNING, JobStore, SynthesisJob
from voicebox.synthesize.service import Speech, SynthesizeRequest

JOB_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CLAIMER, WORKER = 100, 200


def wav_bytes(seconds: float = 0.1, *, rate: int = 22050, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\x00" * int(rate * seconds) * width)
    return buffer.getvalue()


class FakeSynthesizer:
    """Returns a short WAV per chunk, or raises a `SynthesisFailed` at the chunk asked."""

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        kind: str = SynthesisFailed.TIMEOUT,
        audio: bytes | None = None,
    ) -> None:
        self.texts: list[str] = []
        self.voices: list[str | None] = []
        self._fail_at = fail_at
        self._kind = kind
        self._audio = audio

    async def synthesize(self, text: str, *, voice: str | None) -> bytes:
        self.texts.append(text)
        self.voices.append(voice)
        if self._fail_at is not None and len(self.texts) == self._fail_at:
            raise SynthesisFailed("no answer", kind=self._kind)
        return self._audio if self._audio is not None else wav_bytes()


class Liveliness:
    """A speech server whose /health answers come from a list; the last one repeats."""

    def __init__(self, states: Iterable[Liveness]) -> None:
        self._states = list(states)
        self.asked = 0

    async def __call__(self) -> Liveness:
        self.asked += 1
        return self._states[min(self.asked, len(self._states)) - 1]


class CountingSupervisor:
    def __init__(self, *, warm_error: OSError | None = None) -> None:
        self.registered = 0
        self.released = 0
        self.warmed = 0
        self._warm_error = warm_error

    async def register(self) -> None:
        self.registered += 1

    async def release(self) -> bool:
        self.released += 1
        return False

    async def warm(self, reachable: Callable[[], Awaitable[bool]]) -> bool:
        self.warmed += 1
        if self._warm_error is not None:
            raise self._warm_error
        return not await reachable()


def staged(state_dir: Path, text: str, **overrides: object) -> SynthesisJob:
    """A synthesis job as the tool leaves it: record, staged text, lock held for it."""
    fields = dict(
        job_id=JOB_ID,
        state=RUNNING,
        source="",
        output=str(state_dir / "out.wav"),
        format="wav",
        nemo_binary="nemo-speech",
        claimed_by=CLAIMER,
        created_at=1000.0,
        stage="split",
        voice="Sofia",
        tts_model="magpie",
        input_kind="text",
        input_format="plain",
        chars=len(text),
        words=len(text.split()),
        paragraphs=text.count("\n\n") + 1,
        cold_start=False,
    )
    job = SynthesisJob(**{**fields, **overrides})
    jobs = JobStore(state_dir)
    jobs.write(job)
    jobs.text_path(JOB_ID).write_text(text, encoding="utf-8")
    job_lock(state_dir, is_alive=lambda pid: True, pid=CLAIMER).acquire()
    return job


# The synthesize tool's own fakes (tests/test_synthesis_service.py).
ME, OTHER = 100, 300


def desk_at(tmp_path: Path, spawned: list | None = None, alive=(ME,)) -> Desk:
    async def spawn(argv, log) -> int:
        if spawned is not None:
            spawned.append(list(argv))
        return 4242

    async def probe(path: Path) -> Never:
        raise AssertionError("synthesize never probes media")

    state = tmp_path / "state"
    return Desk(
        jobs=JobStore(state),
        lock=lambda: job_lock(state, is_alive=lambda p: p in alive, pid=ME),
        is_alive=lambda p: p in alive,
        spawn=spawn,
        probe=probe,
        clock=lambda: 1000.0,
        pid=ME,
    )


def speech(
    state=Liveness.READY, voices=("John", "Sofia"), have=("ffmpeg", "nemo-speech")
) -> Speech:
    async def liveness() -> Liveness:
        return state

    async def listed() -> list[str]:
        return list(voices)

    return Speech(
        liveness=liveness,
        voices=listed,
        which=lambda name: f"/usr/bin/{name}" if name in have else None,
    )


def settings(tmp_path: Path, **server: object) -> Settings:
    return Settings(state_dir=tmp_path / "state", server=ServerSettings(**server))


def request(**fields: object) -> SynthesizeRequest:
    base = dict(text=None, path=None, voice=None, output=None, format="wav")
    return SynthesizeRequest(**{**base, **fields})
