"""One JSON record per transcription job, under `<state_dir>/transcriptions/`.

The tool writes a record when a job starts and the worker writes it again when
the job ends, so status reads a state rather than inferring one from a pid and a
file.
"""

import json
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path

RUNNING = "running"
DONE = "done"
EMPTY = "empty"  # finished, and the model heard no speech
FAILED = "failed"
CRASHED = "crashed"  # the worker died without writing an outcome

# Realtime multiples measured on an M5, cold, including model load (spec, Goal).
REALTIME_RATIO_DIARIZED = 6.9
REALTIME_RATIO_PLAIN = 9.5


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
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "transcriptions"

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

    def recent(self, limit: int = 10) -> list[Job]:
        if not self._dir.is_dir():
            return []
        jobs = [job for path in self._dir.glob("*.json") if (job := self.read(path.stem))]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    def log_tail(self, job_id: str, lines: int = 8) -> str:
        try:
            text = self.log_path(job_id).read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])
