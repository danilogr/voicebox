"""The transcribe tool, minus the MCP plumbing: validate the file, then start a worker.
A request turned away for a closed-set reason is recorded as a `refused` row."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from voicebox.config import Settings
from voicebox.jobs.desk import Desk, busy_message, claim, duration, launch
from voicebox.jobs.lock import BUSY
from voicebox.jobs.store import RUNNING, TranscriptionJob, new_job_id
from voicebox.programs import ToolMissing
from voicebox.telemetry.rows import TRANSCRIPTION, JobFacts, iso, job_row, wall_ms
from voicebox.telemetry.session import TelemetrySession
from voicebox.transcribe.output import FORMATS, output_path
from voicebox.transcribe.prepare import needs_conversion
from voicebox.transcribe.probe import Probe, ProbeFailed

NO_NEMO = (
    "transcribe needs NVIDIA NeMo-Speech.cpp, and it is not installed. Run /voicebox:setup, "
    "or set NEMO_BIN if it is installed somewhere else."
)


@dataclass(frozen=True)
class TranscribeRequest:
    path: str
    output_dir: str | None
    format: str
    diarize: bool
    language: str | None
    translate_to: str | None


@dataclass(frozen=True)
class _Refusal:
    """Who is answering whom, for the `refused` row a turned-away request leaves."""

    desk: Desk
    settings: Settings
    request: TranscribeRequest
    telemetry: TelemetrySession | None
    began: float
    began_monotonic: float

    def record(self, reason: str, source: Path | None, found: Probe | None = None) -> None:
        if self.telemetry is None:
            return
        now = self.desk.clock()
        self.telemetry.record(
            job_row(
                JobFacts(
                    job_id=new_job_id(),
                    session_id=self.telemetry.session_id,
                    install_id=self.telemetry.install_id,
                    kind=TRANSCRIPTION,
                    started_at=iso(self.began),
                    ended_at=iso(now),
                    wall_ms=wall_ms(
                        self.began,
                        self.began_monotonic,
                        now=now,
                        now_monotonic=self.desk.monotonic(),
                    ),
                    status="refused",
                    reason=reason,
                    engine="nemo",
                    model=self.settings.server.asr_model,
                    source_name=source.name if source is not None else None,
                ),
                {
                    "source_bytes": found.size_bytes if found is not None else None,
                    # Unrounded: `job_row` rounds a finite value and drops one that is not.
                    "audio_ms": found.duration_s * 1000
                    if found is not None and found.has_audio
                    else None,
                    "audio_codec": found.codec if found is not None else None,
                    "has_video": found.has_video if found is not None else None,
                    "diarize": self.request.diarize,
                    "language": self.request.language or "auto",
                    "translate": self.request.translate_to is not None,
                    "format": self.request.format,
                },
            )
        )


async def start(
    desk: Desk,
    settings: Settings,
    request: TranscribeRequest,
    telemetry: TelemetrySession | None = None,
) -> str:
    refusal = _Refusal(desk, settings, request, telemetry, desk.clock(), desk.monotonic())
    try:
        source = _expanded(request.path)
    except RuntimeError as error:
        refusal.record("input_missing", None)
        return f"cannot resolve the path {request.path}: {error}"
    if not source.is_file():
        refusal.record("input_missing", source)
        return f"no such file: {source}"
    if request.format not in FORMATS:
        return f"unknown format {request.format!r}; use one of {', '.join(FORMATS)}"
    try:
        found = await desk.probe(source)
    except ToolMissing as error:
        refusal.record("ffmpeg_missing", source)
        return str(error)
    except ProbeFailed as error:
        refusal.record("input_unsupported", source)
        return f"could not read {source.name}: {error}"
    if not found.has_audio:
        refusal.record("input_unsupported", source, found)
        return (
            f"{source.name} has no audio stream - it is video only, "
            "so there is nothing to transcribe"
        )
    if request.output_dir:
        try:
            directory = _expanded(request.output_dir)
        except RuntimeError as error:
            refusal.record("output_unwritable", source, found)
            return f"cannot resolve the output directory {request.output_dir}: {error}"
        if not directory.is_absolute():
            refusal.record("output_unwritable", source, found)
            return (
                f"output_dir must be an absolute path: {directory} (this server runs from "
                "the plugin's own directory, not the project)"
            )
    else:
        directory = settings.transcribe_dir or source.parent
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        refusal.record("output_unwritable", source, found)
        return f"cannot write transcripts to {directory}"
    if desk.which(settings.server.binary) is None:
        # The worker runs it; finding it missing there would cost a job the user waited on.
        refusal.record("nemo_missing", source, found)
        return NO_NEMO
    try:
        lock = claim(desk)
    except BUSY:
        refusal.record("busy", source, found)
        return busy_message(desk)

    job_id = new_job_id()
    convert = needs_conversion(found)
    job = TranscriptionJob(
        job_id=job_id,
        state=RUNNING,
        source=str(source.resolve()),
        output=str(output_path(source, directory, request.format, job_id)),
        format=request.format,
        nemo_binary=settings.server.binary,
        claimed_by=desk.pid,
        created_at=refusal.began,
        created_monotonic=refusal.began_monotonic,
        stage="convert" if convert else "asr",
        telemetry=telemetry.ref() if telemetry is not None else None,
        diarize=request.diarize,
        language=request.language,
        translate_to=request.translate_to,
        duration_s=found.duration_s,
        size_bytes=found.size_bytes,
        codec=found.codec,
        convert=convert,
        asr_model=settings.server.asr_model,
        has_video=found.has_video,
    )
    argv = [sys.executable, "-m", "voicebox.transcribe.worker", str(settings.state_dir), job_id]
    failure = await launch(desk, lock, job, argv)
    if failure is not None:
        return f"could not start transcription: {failure}"
    return (
        f"Transcribing {source.name} ({duration(found.duration_s)} of audio"
        f"{', labelling speakers' if request.diarize else ''}) to {job.output}\n"
        f"Job {job_id}; about {duration(job.eta_s)}. Ask job_status for progress."
    )


def _expanded(raw: str) -> Path:
    """`~` and `~user` expanded; raises RuntimeError for a home that cannot be found."""
    return Path(raw).expanduser()
