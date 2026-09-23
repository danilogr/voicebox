"""One JSON record per transcription job, under `<state_dir>/transcriptions/`.

The tool writes a record when a job starts and the worker writes it again when
the job ends, so status reads a state rather than inferring one from a pid and a
file.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
EMPTY = "empty"  # finished, and the model heard no speech
FAILED = "failed"
CRASHED = "crashed"  # the worker died without writing an outcome

# Realtime multiples measured on an M5, cold, including model load (spec, Goal).
REALTIME_RATIO_DIARIZED = 6.9
REALTIME_RATIO_PLAIN = 9.5

# How long a finished job's record and log stay in the directory before `recent()`
# prunes them - generous for a human looking back at what they transcribed.
RETENTION_S = 14 * 24 * 60 * 60  # two weeks


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    state: str
    source: str
    output: str
    format: str
    diarize: bool
    language: str | None
    translate_to: str | None
    duration_s: float
    size_bytes: int
    codec: str | None
    convert: bool
    asr_model: str
    nemo_binary: str
    claimed_by: int
    created_at: float
    worker_pid: int | None = None
    finished_at: float | None = None
    error: str | None = None
    words: int | None = None
    chars: int | None = None
    asr_ms: int | None = None
    # {"db_path", "session_id", "install_id"} of the session that asked, or None
    # when that session has telemetry off.
    telemetry: dict[str, str] | None = None

    @property
    def eta_s(self) -> float:
        return eta_seconds(self.duration_s, self.diarize)


def eta_seconds(duration_s: float, diarize: bool) -> float:
    ratio = REALTIME_RATIO_DIARIZED if diarize else REALTIME_RATIO_PLAIN
    return duration_s / ratio


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


class JobStore:
    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._dir = Path(state_dir) / "transcriptions"
        self._clock = clock

    def record_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.log"

    def wav_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.wav"

    def write(self, job: Job) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.record_path(job.job_id)
        partial = path.with_name(f"{job.job_id}.json.partial")
        partial.write_text(json.dumps(asdict(job), indent=1))
        partial.replace(path)  # a reader never sees half a record

    def read(self, job_id: str) -> Job | None:
        try:
            data = json.loads(self.record_path(job_id).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Job(**{f.name: data[f.name] for f in fields(Job) if f.name in data})
        except TypeError:
            return None

    def running(self) -> list[Job]:
        """Every record that still says running - all of them, not a page.

        `recent` answers "what has this install been doing lately" and stops at ten;
        deciding that nothing is running has to read them all, because the eleventh
        record is as able to hold the lock as the first.
        """
        if not self._dir.is_dir():
            return []
        return [
            job
            for path in self._dir.glob("*.json")
            if (job := self.read(path.stem)) and job.state == RUNNING
        ]

    def recent(self, limit: int = 10) -> list[Job]:
        if not self._dir.is_dir():
            return []
        self._prune()
        jobs = [job for path in self._dir.glob("*.json") if (job := self.read(path.stem))]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    def _prune(self) -> None:
        """Reclaim what a finished job no longer needs.

        Any job that is not running loses its converted `.wav` at once - the audio
        is 16 kHz mono s16, about 115 MB per hour, and nothing but this call ever
        reclaims one left behind by a worker that never reached its own `finally`
        (a `SIGKILL`, an OOM kill). A job's record and log outlive it for
        `RETENTION_S` before those go too, so a status check still answers "what
        did I transcribe last week". Never touches a running job, and never raises:
        a `transcription_status` call failing because housekeeping hit a locked or
        missing file would be worse than the clutter it is trying to prevent.
        """
        now = self._clock()
        try:
            for path in self._dir.glob("*.json"):
                job = self.read(path.stem)
                if job is None or job.state == RUNNING:
                    continue
                self.wav_path(job.job_id).unlink(missing_ok=True)
                if job.finished_at is not None and now - job.finished_at >= RETENTION_S:
                    self.record_path(job.job_id).unlink(missing_ok=True)
                    self.log_path(job.job_id).unlink(missing_ok=True)
        except OSError as error:
            logger.debug("could not prune old transcription jobs: %s", error)

    def log_tail(self, job_id: str, lines: int = 8) -> str:
        try:
            text = self.log_path(job_id).read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])
