"""One voicebox job at a time, whatever its kind: transcription and synthesis contend
for the same accelerator."""

from collections.abc import Callable
from pathlib import Path

from speak_core.exclusive import ClaimInProgressError, ExclusiveLock

# How long a lock directory with no owner file is taken for another process mid-claim
# (between its `mkdir` and its owner write) before it is reclaimed as abandoned.
# Reclaiming at once would let two sessions each start a job.
OWNERLESS_GRACE_S = 2.0


class JobBusy(RuntimeError):  # noqa: N818 - reads as a state, not a failure mode
    """Another live process is running a voicebox job."""

    def __init__(self, owner: int) -> None:
        super().__init__(
            f"another voicebox job (pid {owner}) is running; voicebox runs one job at a time"
        )
        self.owner = owner


# Everything `job_lock(...).acquire()` raises for a lock someone else has or is taking.
BUSY = (JobBusy, ClaimInProgressError)


def job_lock(
    state_dir: Path, *, is_alive: Callable[[int], bool], pid: int | None = None
) -> ExclusiveLock:
    return ExclusiveLock(
        directory=Path(state_dir) / "job",
        is_alive=is_alive,
        busy=JobBusy,
        pid=pid,
        # Not re-entrant: the session claims this lock for a worker that does not exist
        # yet, so its own pid in the file means "a job is starting", not "this call
        # already holds it". Two tool calls in one session would otherwise both spawn.
        reentrant=False,
        ownerless_grace=OWNERLESS_GRACE_S,
    )
