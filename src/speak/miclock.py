"""Exclusive claim on the one microphone the machine has.

Sessions are separate processes with no other way to see each other, so they
coordinate through the filesystem the way the speech server already does: an
atomic `mkdir` elects one holder, and a lock whose owner has died is reclaimed.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)


class MicrophoneBusy(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """Another live session is already recording."""

    def __init__(self, owner: int) -> None:
        super().__init__(
            f"another session (pid {owner}) is listening; only one can use the microphone"
        )
        self.owner = owner


class MicLock:
    """One holder at a time, elected by an atomic `mkdir`."""

    def __init__(
        self,
        *,
        state_dir: Path,
        is_alive: Callable[[int], bool],
        pid: int | None = None,
    ) -> None:
        self._dir = Path(state_dir) / "mic"
        self._owner_file = self._dir / "owner"
        self._is_alive = is_alive
        self._pid = pid if pid is not None else os.getpid()

    def owner(self) -> int | None:
        """The pid recorded in the lock, or None when there is none to read."""
        try:
            return int(self._owner_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire(self) -> None:
        """Claim the microphone, or raise `MicrophoneBusy`."""
        self._dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._dir.mkdir()
        except FileExistsError:
            held_by = self.owner()
            if held_by is not None and held_by != self._pid and self._is_alive(held_by):
                raise MicrophoneBusy(held_by) from None
            # Two reclaimers racing could both take over a dead owner's lock. That is
            # the same window the supervisor's stale-start-lock takeover accepts, and
            # the cost is one garbled recording rather than a wedged system.
            if held_by != self._pid:
                logger.info("reclaiming a microphone lock left by %s", held_by)
        self._owner_file.write_text(str(self._pid))

    def release(self) -> None:
        """Give the microphone back. A lock held by someone else is left alone."""
        if self.owner() != self._pid:
            return
        self._owner_file.unlink(missing_ok=True)
        try:
            self._dir.rmdir()
        except OSError as error:
            logger.debug("could not remove the microphone lock: %s", error)

    def __enter__(self) -> "MicLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
