"""Exclusive claim on the one microphone the machine has.

Sessions are separate processes with no other way to see each other, so they
coordinate through the filesystem the way the speech server already does: an
atomic `mkdir` elects one holder, and a lock whose owner has died is reclaimed.
"""

from collections.abc import Callable
from pathlib import Path

from speak_core.exclusive import ExclusiveLock


class MicrophoneBusy(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """Another live session is already recording."""

    def __init__(self, owner: int) -> None:
        super().__init__(
            f"another session (pid {owner}) is listening; only one can use the microphone"
        )
        self.owner = owner


class MicLock(ExclusiveLock):
    """Exclusive claim on the one microphone the machine has."""

    def __init__(
        self,
        *,
        state_dir: Path,
        is_alive: Callable[[int], bool],
        pid: int | None = None,
    ) -> None:
        super().__init__(
            directory=Path(state_dir) / "mic", is_alive=is_alive, busy=MicrophoneBusy, pid=pid
        )
