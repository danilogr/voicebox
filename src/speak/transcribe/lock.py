"""One transcription at a time: two would contend for the same accelerator."""

from collections.abc import Callable
from pathlib import Path

from speak.exclusive import ExclusiveLock


class TranscriptionBusy(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """Another live process is already transcribing."""

    def __init__(self, owner: int) -> None:
        super().__init__(f"another transcription (pid {owner}) is running; only one runs at a time")
        self.owner = owner


def transcription_lock(
    state_dir: Path, *, is_alive: Callable[[int], bool], pid: int | None = None
) -> ExclusiveLock:
    return ExclusiveLock(
        directory=Path(state_dir) / "transcribe",
        is_alive=is_alive,
        busy=TranscriptionBusy,
        pid=pid,
    )
