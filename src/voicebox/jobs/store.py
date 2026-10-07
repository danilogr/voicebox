"""One JSON record per job, whatever its kind, under `<state_dir>/jobs/`.

The tool writes a record when it accepts a job and the detached worker writes every
later version, so status reads a state rather than inferring one from a pid and a file.
"""

import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from voicebox.synthesize.limits import COLD_START_S, PACE_MIN_CHARS, SYNTH_CHARS_PER_S

logger = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
EMPTY = "empty"  # a transcription that finished and heard no speech
FAILED = "failed"
CRASHED = "crashed"  # the worker died without writing an outcome

TRANSCRIPTION = "transcription"
SYNTHESIS = "synthesis"

# Realtime multiples measured on an M5, cold, including model load (performance.md).
REALTIME_RATIO_DIARIZED = 6.9
REALTIME_RATIO_PLAIN = 9.5

# How long a finished job's record and log stay before `recent()` prunes them -
# generous for a human looking back at what they transcribed.
RETENTION_S = 14 * 24 * 60 * 60


@dataclass(frozen=True, kw_only=True)
class Job:
    """What every job records, whatever its kind. Each kind is a subclass."""

    job_id: str
    kind: str
    state: str
    source: str
    output: str
    format: str
    nemo_binary: str
    claimed_by: int
    created_at: float
    worker_pid: int | None = None
    finished_at: float | None = None
    error: str | None = None
    # `time.monotonic()` when the tool accepted the job. That clock is system-wide, so
    # whichever process ends the job measures `wall_ms` against it; `None` (or a reboot
    # in between) falls back to the wall clock.
    created_monotonic: float | None = None
    # Where the worker is - its first stage from acceptance on - and so where a crash
    # happened; and the telemetry reason a failure was classified as.
    stage: str | None = None
    reason: str | None = None
    # {"db_path", "session_id", "install_id"} of the session that asked, or None when
    # that session records nothing.
    telemetry: dict[str, str] | None = None

    @property
    def eta_s(self) -> float:
        """Seconds of work this job was expected to take. Each kind estimates its own."""
        raise NotImplementedError


@dataclass(frozen=True, kw_only=True)
class TranscriptionJob(Job):
    kind: str = TRANSCRIPTION
    diarize: bool
    language: str | None
    translate_to: str | None
    duration_s: float
    size_bytes: int
    codec: str | None
    convert: bool
    asr_model: str
    words: int | None = None
    chars: int | None = None
    asr_ms: int | None = None
    # A video stream that is not cover art was present (the probe reads it anyway).
    has_video: bool = False
    convert_ms: int | None = None

    @property
    def eta_s(self) -> float:
        return eta_seconds(self.duration_s, self.diarize)


@dataclass(frozen=True, kw_only=True)
class SynthesisJob(Job):
    kind: str = SYNTHESIS
    voice: str
    tts_model: str
    input_kind: str  # text | file
    input_format: str  # plain | markdown
    # The spoken text's size, after markup was removed: what the estimate rests on.
    chars: int
    words: int
    paragraphs: int
    # The speech server was not answering when the job was accepted, so the estimate
    # includes starting it.
    cold_start: bool
    chunks: int | None = None  # set by the worker once the text is split
    chunks_done: int = 0
    synth_ms: int = 0
    encode_ms: int | None = None
    audio_ms: int | None = None
    output_bytes: int | None = None
    server_warm: bool | None = None
    # Characters of the requests that have finished; with `synth_ms`, the throughput the
    # estimate follows once one has.
    chars_done: int = 0
    # The slowest pace, in characters a second, of this job's finished requests of at
    # least `PACE_MIN_CHARS`: what later request timeouts follow.
    slowest_chars_per_s: float | None = None
    # The hidden file beside the output that the worker writes before linking it into
    # place; on the record only while it exists, so a crash there can be cleaned up.
    partial: str | None = None

    @property
    def chars_per_s(self) -> float:
        """Characters spoken per second of synthesis so far, or the measured rate until
        `PACE_MIN_CHARS` have been spoken: a few short requests are mostly fixed cost and
        say nothing about the machine's pace."""
        if self.chars_done >= PACE_MIN_CHARS and self.synth_ms:
            return self.chars_done / (self.synth_ms / 1000)
        return SYNTH_CHARS_PER_S

    @property
    def eta_s(self) -> float:
        return self.chars / self.chars_per_s + (COLD_START_S if self.cold_start else 0.0)


# Every kind a record may hold, by the `kind` it is written with.
JOB_TYPES: dict[str, type[Job]] = {TRANSCRIPTION: TranscriptionJob, SYNTHESIS: SynthesisJob}


def eta_seconds(duration_s: float, diarize: bool) -> float:
    ratio = REALTIME_RATIO_DIARIZED if diarize else REALTIME_RATIO_PLAIN
    return duration_s / ratio


def new_job_id() -> str:
    """A dashed uuid4: the job store's id is the telemetry contract's `job_id`."""
    return str(uuid.uuid4())


class JobStore:
    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._dir = Path(state_dir) / "jobs"
        self._clock = clock

    def record_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.log"

    def wav_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.wav"

    def pid_path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.pid"

    def text_path(self, job_id: str) -> Path:
        """The text a synthesis speaks, staged by the tool for its worker."""
        return self._dir / f"{job_id}.txt"

    def work_dir(self, job_id: str) -> Path:
        """Where a synthesis keeps its chunk WAVs until they are joined."""
        return self._dir / f"{job_id}.d"

    def write(self, job: Job) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.record_path(job.job_id)
        partial = path.with_name(f"{job.job_id}.json.partial")
        partial.write_text(json.dumps(asdict(job), indent=1))
        partial.replace(path)  # a reader never sees half a record

    def write_pid(self, job_id: str, pid: int) -> None:
        """The pid the tool's own spawn returned, in a file only the tool ever writes.

        Kept apart from the job record so recording it is never a read-modify-write
        against a worker writing that same record.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        self.pid_path(job_id).write_text(str(pid))

    def read_pid(self, job_id: str) -> int | None:
        """The pid `write_pid` wrote, or `None` for anything short of a clean int."""
        try:
            return int(self.pid_path(job_id).read_text().strip())
        except (OSError, ValueError):
            return None

    def read(self, job_id: str) -> Job | None:
        try:
            data = json.loads(self.record_path(job_id).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        job_type = JOB_TYPES.get(kind) if isinstance(kind, str) else None
        if job_type is None:
            return None
        try:
            job = job_type(**{f.name: data[f.name] for f in fields(job_type) if f.name in data})
        except TypeError:
            return None
        # Every path a job's artifacts live at is built from its id: a record that names
        # another id than its own file's would point deletes and writes somewhere else.
        return job if job.job_id == job_id else None

    def running(self) -> list[Job]:
        """Every record that still says running - all of them, not a page: deciding that
        nothing is running has to read them all."""
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

    def discard_artifacts(self, job_id: str) -> None:
        """Delete what a job holds only while it runs - converted audio, staged text,
        chunk WAVs, a partly written output - never its record or log. Never raises."""
        try:
            self.wav_path(job_id).unlink(missing_ok=True)
            self.text_path(job_id).unlink(missing_ok=True)
            job = self.read(job_id)
            if isinstance(job, SynthesisJob) and job.partial:
                self._discard_partial(job, job.partial)
        except (OSError, TypeError) as error:
            # TypeError: a hand-edited record whose `partial` is not a path.
            logger.debug("could not discard the artifacts of job %s: %s", job_id, error)
        shutil.rmtree(self.work_dir(job_id), ignore_errors=True)

    def _discard_partial(self, job: SynthesisJob, recorded: str) -> None:
        """Delete the hidden file a synthesis was writing its output to, and take it off
        the record. Only a name the worker would have made is deleted: the path comes
        from a file on disk, and must never reach anything else of the user's."""
        partial, output = Path(recorded), Path(job.output)
        prefix = f".{output.name}."
        if (
            partial.parent == output.parent
            and partial.name.startswith(prefix)
            and partial.name.endswith(".partial")
            and len(partial.name) > len(prefix) + len(".partial")
        ):
            partial.unlink(missing_ok=True)
        else:
            logger.warning("not deleting %s: not a partial output of %s", partial, output)
        self.write(replace(job, partial=None))

    def _prune(self) -> None:
        """Reclaim what a finished job no longer needs.

        Any job that is not running loses its artifacts at once - nothing but this call
        reclaims ones a worker left behind when it never reached its own `finally` (a
        SIGKILL, an OOM kill). Its record, log and pid sidecar outlive it for
        `RETENTION_S`. Never touches a running job and never raises.
        """
        now = self._clock()
        try:
            for path in self._dir.glob("*.json"):
                job = self.read(path.stem)
                if job is None or job.state == RUNNING:
                    continue
                self.discard_artifacts(job.job_id)
                if job.finished_at is not None and now - job.finished_at >= RETENTION_S:
                    self.record_path(job.job_id).unlink(missing_ok=True)
                    self.log_path(job.job_id).unlink(missing_ok=True)
                    self.pid_path(job.job_id).unlink(missing_ok=True)
        except (OSError, TypeError) as error:
            logger.debug("could not prune old jobs: %s", error)

    def log_tail(self, job_id: str, lines: int = 8) -> str:
        try:
            text = self.log_path(job_id).read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])
