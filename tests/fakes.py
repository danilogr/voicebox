"""Fakes shared by the server-level tests."""

import asyncio

from speak_core.engines.base import Utterance
from speak_core.queue import QueueStats


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

    async def warm(self, reachable) -> bool:
        """`NemoSupervisor.warm`: start only when nothing answers on the port."""
        if await reachable():
            return False
        return await self.ensure_running()
