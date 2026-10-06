"""Fakes shared by the server-level tests."""

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path

from speak_core.engines.asr import Liveness
from speak_core.engines.base import Utterance
from speak_core.queue import QueueStats

from speak.capture import Recording, TrackerSettings
from speak.miclock import MicrophoneBusy


class FakeEngine:
    name = "fake"

    def __init__(
        self,
        voices: list[str] | None = None,
        default_voice: str | None = None,
        random_pool: tuple[str, ...] | None = None,
    ) -> None:
        self.default_voice = default_voice
        self.random_pool = random_pool
        self._voices = voices or []
        self.prepared: list[tuple[str, str | None, float | None]] = []

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance:
        self.prepared.append((text, voice, rate))
        return Utterance(command="true", args=[text])

    async def list_voices(self) -> list[str]:
        return list(self._voices)


class FakeQueue:
    def __init__(
        self,
        ahead: int = 0,
        dropped: int = 0,
        calls: list[str] | None = None,
        stats: QueueStats | None = None,
    ) -> None:
        self.enqueued: list[tuple[str, str | None, float | None]] = []
        self.intents: list[str | None] = []
        self.stopped = 0
        self.calls = calls if calls is not None else []
        self._ahead = ahead
        self._dropped = dropped
        self._stats = (
            stats
            if stats is not None
            else QueueStats(waiting=0, preparing=0, prepared=0, playing=False, finished=0)
        )

    def enqueue(
        self, text: str, *, voice: str | None, rate: float | None, intent: str | None = None
    ) -> int:
        self.enqueued.append((text, voice, rate))
        self.intents.append(intent)
        return self._ahead

    async def stop(self) -> int:
        self.stopped += 1
        return self._dropped

    async def run(self) -> None:
        return None

    async def drain(self) -> None:
        self.calls.append("drain")

    def stats(self) -> QueueStats:
        return self._stats


class FakeSupervisor:
    def __init__(self, blocked_by: asyncio.Event | None = None) -> None:
        self.registrations = 0
        self.releases = 0
        self.starts = 0
        self._blocked_by = blocked_by

    async def register(self) -> None:
        self.registrations += 1

    async def release(self) -> bool:
        self.releases += 1
        return True

    async def ensure_running(self) -> bool:
        """A start that only finishes once the gate opens, standing in for a cold load."""
        self.starts += 1
        if self._blocked_by is not None:
            await self._blocked_by.wait()
        return True


class FakeRecorder:
    def __init__(self, recording: Recording, calls: list[str] | None = None) -> None:
        self.recording = recording
        self.calls = calls if calls is not None else []
        self.limits: TrackerSettings | None = None

    async def record(self, settings: TrackerSettings) -> Recording:
        self.calls.append("record")
        self.limits = settings
        return self.recording


class FakeTranscriber:
    def __init__(
        self,
        text: str = "",
        has_asr: bool = True,
        error: Exception | None = None,
        reachable: bool = False,
        starting: bool = False,
    ) -> None:
        self.text = text
        self.languages: list[str | None] = []
        self._has_asr = has_asr
        self._error = error
        self._reachable = reachable
        self._starting = starting
        self.calls: list[str] = []

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> str:
        self.calls.append("transcribe")
        self.languages.append(language)
        if self._error is not None:
            raise self._error
        return self.text

    async def has_asr(self) -> bool:
        return self._has_asr

    async def reachable(self) -> bool:
        return self._reachable

    def becomes_ready(self) -> None:
        """The server has answered — what `ensure_running`'s probe waits for."""
        self._reachable = True
        self._starting = False

    async def liveness(self) -> Liveness:
        if not self._reachable:
            return Liveness.ABSENT
        return Liveness.STARTING if self._starting else Liveness.READY


class FakeMicLock:
    def __init__(self, busy_with: int | None = None) -> None:
        self.busy_with = busy_with
        self.held = False
        self.acquires = 0
        self.releases = 0

    def acquire(self) -> None:
        self.acquires += 1
        if self.busy_with is not None:
            raise MicrophoneBusy(self.busy_with)
        self.held = True

    def release(self) -> None:
        self.releases += 1
        self.held = False


class FinishedSound:
    """A player process that has already exited by the time it is waited on."""

    def __init__(self, code: int, done: Callable[[], None]) -> None:
        self._code = code
        self._done = done

    def terminate(self) -> None:
        return None

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self._done()
        return self._code


class FakePlayer:
    """Stands in for `afplay`: records what it was asked to play, and plays nothing.

    The label lands in `calls` when the player exits rather than when it is spawned,
    so a shared call list pins that a cue had finished before recording started.
    """

    def __init__(
        self,
        calls: list[str] | None = None,
        *,
        code: int = 0,
        error: OSError | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.commands: list[tuple[str, list[str]]] = []
        self._code = code
        self._error = error

    async def spawn(self, command: str, args: Sequence[str]) -> FinishedSound:
        self.commands.append((command, list(args)))
        if self._error is not None:
            raise self._error
        label = Path(args[0]).stem
        return FinishedSound(self._code, lambda: self.calls.append(label))
