"""The transcribe tool, minus the MCP plumbing: validate the file, then start a worker."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from voicebox.config import Settings
from voicebox.jobs.desk import Desk, busy_message, claim, duration, launch
from voicebox.jobs.lock import BUSY
from voicebox.jobs.store import RUNNING, TranscriptionJob, new_job_id
from voicebox.transcribe.commands import ToolMissing
from voicebox.transcribe.output import FORMATS, output_path
from voicebox.transcribe.prepare import needs_conversion
from voicebox.transcribe.probe import ProbeFailed


@dataclass(frozen=True)
class TranscribeRequest:
    path: str
    output_dir: str | None
    format: str
    diarize: bool
    language: str | None
    translate_to: str | None


async def start(desk: Desk, settings: Settings, request: TranscribeRequest) -> str:
    source = Path(request.path).expanduser()
    if not source.is_file():
        return f"no such file: {source}"
    if request.format not in FORMATS:
        return f"unknown format {request.format!r}; use one of {', '.join(FORMATS)}"
    try:
        found = await desk.probe(source)
    except ToolMissing as error:
        return str(error)
    except ProbeFailed as error:
        return f"could not read {source.name}: {error}"
    if not found.has_audio:
        return (
            f"{source.name} has no audio stream - it is video only, "
            "so there is nothing to transcribe"
        )
    directory = _destination(request.output_dir, settings.transcribe_dir, source)
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        return f"cannot write transcripts to {directory}"
    try:
        lock = claim(desk)
    except BUSY:
        return busy_message(desk)

    job_id = new_job_id()
    job = TranscriptionJob(
        job_id=job_id,
        state=RUNNING,
        source=str(source.resolve()),
        output=str(output_path(source, directory, request.format, job_id)),
        format=request.format,
        nemo_binary=settings.server.binary,
        claimed_by=desk.pid,
        created_at=desk.clock(),
        diarize=request.diarize,
        language=request.language,
        translate_to=request.translate_to,
        duration_s=found.duration_s,
        size_bytes=found.size_bytes,
        codec=found.codec,
        convert=needs_conversion(found),
        asr_model=settings.server.asr_model,
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


def _destination(argument: str | None, configured: Path | None, source: Path) -> Path:
    if argument:
        return Path(argument).expanduser()
    return configured if configured is not None else source.parent
