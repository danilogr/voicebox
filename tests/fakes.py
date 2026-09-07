"""Fakes shared by the server-level tests."""

from speak.engines.base import Utterance


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
    def __init__(self, ahead: int = 0, dropped: int = 0) -> None:
        self.enqueued: list[tuple[str, str | None, float | None]] = []
        self.stopped = 0
        self._ahead = ahead
        self._dropped = dropped

    def enqueue(self, text: str, *, voice: str | None, rate: float | None) -> int:
        self.enqueued.append((text, voice, rate))
        return self._ahead

    async def stop(self) -> int:
        self.stopped += 1
        return self._dropped

    async def run(self) -> None:
        return None


class FakeSupervisor:
    def __init__(self) -> None:
        self.registrations = 0
        self.releases = 0

    async def register(self) -> None:
        self.registrations += 1

    async def release(self) -> bool:
        self.releases += 1
        return True
