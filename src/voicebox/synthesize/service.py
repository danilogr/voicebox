"""The synthesize tool, minus the MCP plumbing: check everything that can be checked
before work starts, stage the spoken text, and start the worker. A request turned away
for a closed-set reason is recorded as a `refused` row; one the model only has to ask
differently (both or neither of text and path, no output for pasted text) is answered
and not recorded."""

import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from speak_core.engines.asr import Liveness

from voicebox.config import Settings
from voicebox.jobs.desk import Desk, busy_message, claim, duration, launch, record_unstarted
from voicebox.jobs.lock import BUSY
from voicebox.jobs.store import RUNNING, SynthesisJob, new_job_id
from voicebox.programs import FFMPEG_REMEDY
from voicebox.synthesize.encode import FORMATS
from voicebox.synthesize.text import (
    MARKDOWN_SUFFIXES,
    looks_like_markdown,
    paragraphs,
    strip_markdown,
    unspeakable,
)
from voicebox.telemetry.rows import SYNTHESIS, JobFacts, iso, job_row, recorded_voice, wall_ms
from voicebox.telemetry.session import TelemetrySession

DEFAULT_VOICE = "John"
AUDIO_SUFFIXES = frozenset(f".{fmt}" for fmt in FORMATS)

NO_NEMO = (
    "synthesize needs the local speech server, NVIDIA NeMo-Speech.cpp, and it is not "
    "installed. Run /voicebox:setup, or set NEMO_BIN if it is installed somewhere else."
)
NO_AUTOSTART = (
    "the speech server is not running, and NEMO_AUTOSTART=0 says voicebox must not start "
    "it. Start it yourself, or unset NEMO_AUTOSTART."
)


@dataclass(frozen=True)
class SynthesizeRequest:
    text: str | None
    path: str | None
    voice: str | None
    output: str | None
    format: str


@dataclass(frozen=True)
class Speech:
    """What synthesize asks of the speech server, and of the machine, before it accepts
    a job."""

    liveness: Callable[[], Awaitable[Liveness]]
    voices: Callable[[], Awaitable[list[str]]]
    which: Callable[[str], str | None] = shutil.which


class Refused(Exception):  # noqa: N818 - control flow: a request turned away, not a fault
    """A request turned away before work started. `reason` is the contract's, or None
    for a request the model only has to ask differently (not recorded)."""

    def __init__(self, reason: str | None, message: str, shape: dict[str, object]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.shape = dict(shape)


@dataclass(frozen=True)
class _Prepared:
    text: str
    output: Path
    voice: str
    input_kind: str
    input_format: str
    chars: int
    words: int
    paragraphs: int
    cold_start: bool
    # The synthesis fields a row about this request may carry.
    shape: dict[str, object]


async def start(
    desk: Desk,
    speech: Speech,
    settings: Settings,
    request: SynthesizeRequest,
    telemetry: TelemetrySession | None = None,
) -> str:
    began, began_monotonic = desk.clock(), desk.monotonic()
    job_id = new_job_id()

    def refuse(reason: str, shape: dict[str, object], source: Path | None) -> None:
        if telemetry is None:
            return
        now = desk.clock()
        telemetry.record(
            job_row(
                JobFacts(
                    job_id=new_job_id(),
                    session_id=telemetry.session_id,
                    install_id=telemetry.install_id,
                    kind=SYNTHESIS,
                    started_at=iso(began),
                    ended_at=iso(now),
                    wall_ms=wall_ms(
                        began, began_monotonic, now=now, now_monotonic=desk.monotonic()
                    ),
                    status="refused",
                    reason=reason,
                    engine="nemo",
                    model=settings.server.tts_model,
                    source_name=source.name if source is not None else None,
                ),
                shape,
            )
        )

    try:
        source = _expanded(request.path) if request.path is not None else None
    except RuntimeError as error:
        refuse("input_missing", {}, None)
        return f"cannot resolve the path {request.path}: {error}"
    try:
        prepared = await _prepare(speech, settings, request, source, job_id)
    except Refused as refusal:
        if refusal.reason is not None:
            refuse(refusal.reason, refusal.shape, source)
        return refusal.message
    try:
        lock = claim(desk)
    except BUSY:
        refuse("busy", prepared.shape, source)
        return busy_message(desk)
    job = SynthesisJob(
        job_id=job_id,
        state=RUNNING,
        source=str(source.resolve()) if source is not None else "",
        output=str(prepared.output),
        format=request.format,
        nemo_binary=settings.server.binary,
        claimed_by=desk.pid,
        created_at=began,
        created_monotonic=began_monotonic,
        stage="split",
        telemetry=telemetry.ref() if telemetry is not None else None,
        voice=prepared.voice,
        tts_model=settings.server.tts_model,
        input_kind=prepared.input_kind,
        input_format=prepared.input_format,
        chars=prepared.chars,
        words=prepared.words,
        paragraphs=prepared.paragraphs,
        cold_start=prepared.cold_start,
    )
    text_path = desk.jobs.text_path(job_id)
    try:
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(prepared.text, encoding="utf-8")
    except OSError as error:
        lock.release()
        desk.jobs.discard_artifacts(job_id)
        record_unstarted(desk, job, error)
        return f"could not start synthesis: {error}"
    argv = [sys.executable, "-m", "voicebox.synthesize.worker", str(settings.state_dir), job_id]
    failure = await launch(desk, lock, job, argv)
    if failure is not None:
        text_path.unlink(missing_ok=True)
        return f"could not start synthesis: {failure}"
    name = source.name if source is not None else "the text"
    return (
        f"Writing {name} ({job.words} words) in voice {job.voice} to {job.output}\n"
        f"Job {job_id}; about {duration(job.eta_s)}. Ask job_status for progress."
    )


async def _prepare(
    speech: Speech,
    settings: Settings,
    request: SynthesizeRequest,
    source: Path | None,
    job_id: str,
) -> _Prepared:
    if (request.text is None) == (source is None):
        raise Refused(None, "give synthesize exactly one of text or path", {})
    if request.format not in FORMATS:
        raise Refused(
            None, f"unknown format {request.format!r}; use one of {', '.join(FORMATS)}", {}
        )
    input_kind = "file" if source is not None else "text"
    shape: dict[str, object] = {"format": request.format, "input_kind": input_kind}
    if source is not None:
        raw = _read(source, shape)
        markdown = source.suffix.lower() in MARKDOWN_SUFFIXES
    else:
        raw = request.text or ""
        problem = unspeakable(raw)
        if problem is not None:
            raise Refused("input_unsupported", f"this text cannot be spoken: {problem}", shape)
        markdown = looks_like_markdown(raw)
    input_format = "markdown" if markdown else "plain"
    shape["input_format"] = input_format
    spoken = paragraphs(strip_markdown(raw) if markdown else raw)
    if not spoken:
        once = " once its markup is removed" if markdown else ""
        raise Refused("input_unsupported", f"nothing to say: the text is empty{once}", shape)
    text = "\n\n".join(spoken)
    chars, words = len(text), len(text.split())
    shape.update(chars=chars, words=words, paragraphs=len(spoken))

    output = _output(request, source, job_id, shape)
    if request.format != "wav" and speech.which("ffmpeg") is None:
        raise Refused(
            "ffmpeg_missing",
            f"{request.format} needs ffmpeg, which is not installed; {FFMPEG_REMEDY}, "
            "or ask for wav",
            shape,
        )
    state = await speech.liveness()
    if state is Liveness.ABSENT:
        if speech.which(settings.server.binary) is None:
            raise Refused("nemo_missing", NO_NEMO, shape)
        if not settings.server.autostart:
            raise Refused("server_unavailable", NO_AUTOSTART, shape)
    voice = await _voice(speech, settings, request, state, shape)
    return _Prepared(
        text=text,
        output=output,
        voice=voice,
        input_kind=input_kind,
        input_format=input_format,
        chars=chars,
        words=words,
        paragraphs=len(spoken),
        cold_start=state is not Liveness.READY,
        shape=shape,
    )


def _expanded(raw: str) -> Path:
    """`~` and `~user` expanded; raises RuntimeError for a home that cannot be found."""
    return Path(raw).expanduser()


def _read(source: Path, shape: dict[str, object]) -> str:
    if not source.is_absolute():
        raise Refused(
            "input_missing",
            f"path must be absolute: {source} (this server runs from the plugin's own "
            "directory, not the project)",
            shape,
        )
    if not source.is_file():
        raise Refused("input_missing", f"no such file: {source}", shape)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise Refused("input_missing", f"cannot read {source}: {error}", shape) from None
    if b"\x00" in data:
        raise Refused("input_unsupported", f"{source.name} is not a text file", shape)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Refused("input_unsupported", f"{source.name} is not UTF-8 text", shape) from None


def _output(
    request: SynthesizeRequest, source: Path | None, job_id: str, shape: dict[str, object]
) -> Path:
    suffix = f".{request.format}"
    if request.output is not None:
        try:
            output = _expanded(request.output)
        except RuntimeError as error:
            raise Refused(
                "output_unwritable",
                f"cannot resolve the output path {request.output}: {error}",
                shape,
            ) from None
        if not output.is_absolute():
            raise Refused("output_unwritable", f"output must be an absolute path: {output}", shape)
        if output.suffix.lower() in AUDIO_SUFFIXES and output.suffix.lower() != suffix:
            raise Refused(
                "output_unwritable",
                f"{output.name} ends in {output.suffix} but the format is {request.format}; "
                "change one of them",
                shape,
            )
        if _taken(output):
            raise Refused(
                "output_unwritable",
                f"{output} already exists, and voicebox never overwrites a file; choose "
                "another path or delete it first",
                shape,
            )
    elif source is None:
        raise Refused(
            None,
            "with text, give output: an absolute path for the audio file (this server runs "
            "from the plugin's own directory, not the project)",
            shape,
        )
    else:
        output = source.with_suffix(suffix)
        if _taken(output):
            output = source.with_name(f"{source.stem}.{job_id[:8]}{suffix}")
            if _taken(output):
                raise Refused(
                    "output_unwritable",
                    f"{output} already exists, and voicebox never overwrites a file; "
                    "give output with another path",
                    shape,
                )
    if not output.parent.is_dir() or not os.access(output.parent, os.W_OK):
        raise Refused(
            "output_unwritable",
            f"cannot write audio to {output.parent}; give output with a path elsewhere",
            shape,
        )
    return output


def _taken(path: Path) -> bool:
    """Something is at `path`, a dangling symlink included: writing there would follow
    the link and create what it points at."""
    return path.exists() or path.is_symlink()


async def _voice(
    speech: Speech,
    settings: Settings,
    request: SynthesizeRequest,
    state: Liveness,
    shape: dict[str, object],
) -> str:
    wanted = (request.voice or settings.speech.voice or DEFAULT_VOICE).strip()
    shape["voice"] = recorded_voice("nemo", wanted)
    if state is not Liveness.READY:
        # Nothing to check against yet; a voice the model does not have fails the job at
        # its first chunk, with the server's own answer.
        return wanted
    available = await speech.voices()
    match = next((voice for voice in available if voice.lower() == wanted.lower()), None)
    if available and match is None:
        raise Refused(
            "input_unsupported",
            f'unknown voice "{wanted}"; available: {", ".join(available)}',
            shape,
        )
    return match or wanted
