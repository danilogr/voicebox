"""Hand speech to an arbitrary command: piper, espeak, a script, a remote speaker."""

from speak.engines.base import Utterance


class CommandEngine:
    """Runs a configured command with the text appended as its final argument."""

    name = "command"
    random_pool: tuple[str, ...] | None = None

    def __init__(self, command: str | None, *, default_voice: str | None = None) -> None:
        parts = (command or "").split()
        if not parts:
            raise ValueError("the command engine needs a command line to run")
        self._binary, *self._prefix = parts
        self.default_voice = default_voice

    async def prepare(self, text: str, *, voice: str | None, rate: float | None) -> Utterance:
        # The command is never told the voice, so none is reported.
        return Utterance(command=self._binary, args=[*self._prefix, text], engine=self.name)

    async def list_voices(self) -> list[str]:
        return []
