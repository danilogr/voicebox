"""Who may make sound right now, across every process that plays speech.

Each plugin runs its own playback queue in its own process, and nothing in one queue
can see another. They coordinate through a lock in the shared state directory, held
around each utterance's playback, so sentences from two plugins interleave instead of
overlapping. Election is the atomic `mkdir` the speech server's start lock and the
microphone lock use, and a lock whose holder has died is reclaimed.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from speak.exclusive import ExclusiveLock
from speak.supervisor import process_is_alive

logger = logging.getLogger(__name__)

LOCK_DIRNAME = "speaker"
WAITING_DIRNAME = "speaker-waiting"

# How often a waiting queue tries the lock again. A stop does not wait for the next
# try: the wait ends as soon as the queue's stop event is set.
POLL_INTERVAL = 0.05

# How long a process that has just finished an utterance stays out of the race while
# another process waits. Without it the holder, whose next utterance is usually already
# prepared, takes the lock back within milliseconds and a waiter that polls never gets
# a turn. Several poll intervals, so a waiter asleep mid-poll still wakes inside it.
HANDOFF_WINDOW = 0.25


class SpeakerBusyError(RuntimeError):
    """Another live process is playing speech."""

    def __init__(self, owner: int) -> None:
        super().__init__(f"process {owner} is playing speech")
        self.owner = owner


class SpeakerLock:
    """The machine's speakers, claimed for one utterance at a time across processes.

    Not re-entrant: a second claim from the holder's own process is a second
    utterance, which waits like any other. A holder whose pid the OS has reused for a
    live process looks alive, the same limitation the supervisor's pid record has.
    """

    def __init__(
        self,
        *,
        shared_dir: Path,
        is_alive: Callable[[int], bool] = process_is_alive,
        pid: int | None = None,
        poll: float = POLL_INTERVAL,
    ) -> None:
        self._pid = pid if pid is not None else os.getpid()
        self._is_alive = is_alive
        self._poll = poll
        self._lock = ExclusiveLock(
            directory=Path(shared_dir) / LOCK_DIRNAME,
            is_alive=is_alive,
            busy=SpeakerBusyError,
            pid=self._pid,
            reentrant=False,
        )
        self._waiting_dir = Path(shared_dir) / WAITING_DIRNAME
        self._yield_until = 0.0
        self._warned = False

    async def acquire(self, stopped: asyncio.Event) -> bool:
        """Wait for the speakers. True once they are this process's; False when
        `stopped` is set first.

        A shared directory that cannot be used is no reason to stay silent: the
        failure is logged and the utterance plays uncoordinated.
        """
        marker = self._waiting_dir / str(self._pid)
        marked: bool | None = None
        try:
            while not stopped.is_set():
                if not self._yielding() and self._try_lock():
                    return True
                if marked is None:
                    marked = self._mark(marker)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=self._poll)
                except TimeoutError:
                    continue
            return False
        finally:
            if marked:
                self._unmark(marker)

    def release(self) -> None:
        """Give the speakers back, and stand aside briefly if another process waits."""
        if self._lock.owner() != self._pid:
            return
        self._lock.release()
        if self._others_waiting():
            self._yield_until = time.monotonic() + HANDOFF_WINDOW

    def _try_lock(self) -> bool:
        try:
            self._lock.acquire()
        except SpeakerBusyError:
            return False
        except FileNotFoundError:
            # The holder released between this process's `mkdir` failing and its owner
            # file being written: a handoff race, not a broken directory (the parent is
            # created first). Try again at the next poll.
            return False
        except OSError as error:
            # Once per lock: a broken directory fails every utterance the same way.
            log = logger.debug if self._warned else logger.warning
            self._warned = True
            log("cannot coordinate speech with other processes, playing anyway: %s", error)
        return True

    def _yielding(self) -> bool:
        return time.monotonic() < self._yield_until and self._others_waiting()

    def _others_waiting(self) -> bool:
        """True when a live process other than this one waits. Markers left by
        processes that died are removed on the way."""
        try:
            entries = list(self._waiting_dir.iterdir())
        except OSError:
            return False
        waiting = False
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == self._pid:
                continue
            if self._is_alive(int(entry.name)):
                waiting = True
            else:
                self._unmark(entry)
        return waiting

    def _mark(self, marker: Path) -> bool:
        try:
            self._waiting_dir.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as error:
            logger.debug("could not mark this process as waiting for the speakers: %s", error)
            return False
        return True

    def _unmark(self, marker: Path) -> None:
        try:
            marker.unlink(missing_ok=True)
        except OSError as error:
            logger.debug("could not remove %s: %s", marker, error)
