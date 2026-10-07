"""Fakes for the synthesis tests: a speech server that answers as told, a supervisor
that counts, and a staged job ready for its worker."""

import io
import wave
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from speak_core.engines.asr import Liveness
from speak_core.engines.nemo import SynthesisFailed

from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import RUNNING, JobStore, SynthesisJob

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
