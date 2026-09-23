"""One holder at a time, across processes.

Sessions are separate processes with no other way to see each other, so they
coordinate through the filesystem the way the speech server already does: an
atomic `mkdir` elects one holder, and a lock whose owner has died is reclaimed.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self

logger = logging.getLogger(__name__)


class ExclusiveLock:
    """One holder at a time, elected by an atomic `mkdir`, released only by its holder.

    Re-entrant by default: the holder asking again is a session claiming something
    it already has, and refusing it would fail a retry that is no contention at
    all. A lock one process claims on another's behalf passes `reentrant=False`,
    because there the pid in the file is not the work: two calls inside one process
    would both find their own pid and both believe they had won.
    """

    def __init__(
        self,
        *,
        directory: Path,
        is_alive: Callable[[int], bool],
        busy: Callable[[int], Exception],
        pid: int | None = None,
        reentrant: bool = True,
    ) -> None:
        self._dir = Path(directory)
        self._owner_file = self._dir / "owner"
        self._is_alive = is_alive
        self._busy = busy
        self._pid = pid if pid is not None else os.getpid()
        self._reentrant = reentrant

    def owner(self) -> int | None:
        """The pid recorded in the lock, or None when there is none to read."""
        try:
            return int(self._owner_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire(self) -> None:
        """Claim the lock, or raise whatever `busy` builds for the live holder."""
        self._dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._dir.mkdir()
        except FileExistsError:
            held_by = self.owner()
            ours = held_by == self._pid and self._reentrant
            if held_by is not None and not ours and self._is_alive(held_by):
                raise self._busy(held_by) from None
            # Two reclaimers racing could both take over a dead owner's lock: the
            # window the supervisor's stale-start-lock takeover already accepts.
            # There is a second window here too, with no dead owner involved at
            # all: between another process's `mkdir` and its own `_write_owner()`,
            # `owner()` reads None because there is no owner file yet, and that
            # reads the same as no owner ever having claimed the lock - so a
            # process racing it in that gap reclaims a lock that was just taken.
            # Inherited from MicLock, where the cost of losing this race was one
            # garbled recording; for a transcription job it is two jobs running
            # at once.
            if held_by != self._pid:
                logger.info("reclaiming the %s lock left by %s", self._dir.name, held_by)
        self._write_owner()

    def take_over(self, previous: int) -> None:
        """Become the holder of a lock `previous` claimed on this process's behalf.

        The receiver takes over rather than the claimer handing over, because the
        claimer cannot name a process that does not exist yet, and one that fails
        at once would otherwise try to release in the gap before the handover.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        held_by = self.owner()
        if held_by not in (None, previous, self._pid) and self._is_alive(held_by):
            raise self._busy(held_by)
        self._write_owner()

    def release(self) -> None:
        """Give the lock back. A lock held by someone else is left alone."""
        if self.owner() != self._pid:
            return
        # Not atomic: a take_over() landing between the check above and this unlink
        # loses the new holder's claim, and an unrelated process could then acquire
        # the "free" lock while that holder is still running, so callers release
        # only when no takeover can be in flight — the same class of window acquire() accepts.
        self._owner_file.unlink(missing_ok=True)
        try:
            self._dir.rmdir()
        except OSError as error:
            logger.debug("could not remove the %s lock: %s", self._dir.name, error)

    def _write_owner(self) -> None:
        # Replaced, not rewritten: a reader between a truncate and a write would
        # see no owner and reclaim a lock that is held.
        partial = self._owner_file.with_name(f"owner.{self._pid}")
        partial.write_text(str(self._pid))
        partial.replace(self._owner_file)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
