"""The detached process that owns one synthesis job: split the staged text, synthesize
each chunk, join and encode, and write the file.

Run as `python -m voicebox.synthesize.worker <state_dir> <job_id>` in its own session,
so the job outlives the MCP session that asked for it. When the speech server is
voicebox's to start (`NEMO_AUTOSTART`), the worker registers with its supervisor for
its whole life - so another session ending cannot stop the server mid-job - and
releases that registration when it ends, stopping the server if nobody else needs it.
It is also what records the job's telemetry row.
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
import wave
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from speak_core.config import ServerSettings
from speak_core.engines.asr import Liveness, NemoTranscriber
from speak_core.engines.nemo import NemoSynthesizer, SynthesisFailed
from speak_core.runtime import supervisor_for
from speak_core.supervisor import process_is_alive

from voicebox.jobs.desk import Record, start_next_job
from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import DONE, FAILED, QUEUED, Job, JobStore, SynthesisJob
from voicebox.programs import ConversionFailed, ToolMissing
from voicebox.synthesize.audio import (
    FormatMismatchError,
    OutputTooLargeError,
    Piece,
    concatenate,
)
from voicebox.synthesize.encode import encode as encode_audio
from voicebox.synthesize.limits import (
    CHUNK_CHARS,
    PACE_MIN_CHARS,
    WARMUP_TIMEOUT_S,
    request_timeout_s,
)
from voicebox.synthesize.text import chunk_text
from voicebox.telemetry.jobs import record_job

logger = logging.getLogger(__name__)

# Pauses inserted between chunks: short inside a paragraph, longer between paragraphs,
# so the file reads as one continuous voice rather than a list of clips.
CHUNK_GAP_MS = 250
PARAGRAPH_GAP_MS = 700

WARMUP_POLL_S = 0.5

# The worker bounds each request itself (`request_timeout_s`); the HTTP client's own
# limit is only a backstop, and a request that reaches it fails as a timeout too.
CLIENT_TIMEOUT_S = 24 * 60 * 60.0

# A request shorter than this is timed as this long, so a pace is never infinite.
MIN_REQUEST_S = 0.001

REASON_OF_FAILURE = {
    SynthesisFailed.UNREACHABLE: "server_unavailable",
    SynthesisFailed.TIMEOUT: "timeout",
    SynthesisFailed.REFUSED: "model_failed",
}

Encode = Callable[[Path, Path, str], Awaitable[None]]
# Awaits a request for at most a number of seconds, raising TimeoutError past it.
Within = Callable[[Awaitable[bytes], float], Awaitable[bytes]]
CheckLiveness = Callable[[], Awaitable[Liveness]]


class Synthesizer(Protocol):
    async def synthesize(self, text: str, *, voice: str | None) -> bytes: ...


class Supervisor(Protocol):
    async def register(self) -> None: ...

    async def release(self) -> bool: ...

    async def warm(self, reachable: Callable[[], Awaitable[bool]]) -> bool: ...


@dataclass(frozen=True)
class WorkerSpeech:
    """The speech server as the worker uses it."""

    synthesizer: Synthesizer
    liveness: CheckLiveness
    # None when NEMO_AUTOSTART is off: the server is the user's to run.
    supervisor: Supervisor | None


def place(
    source: Path, destination: Path, *, on_partial: Callable[[Path], None] = lambda path: None
) -> None:
    """Put a copy of `source` at `destination`, whole, and only if nothing is there.

    The bytes go to a new hidden file beside `destination` first, so nobody ever finds
    half a file at the path they were promised; `on_partial` is told its path before
    anything is written to it. That file is then hard-linked into place, which fails
    when `destination` exists: a file that appeared there while the job ran is never
    replaced. On a filesystem without hard links (FAT and exFAT have none) an empty file
    is created exclusively at `destination` and the hidden file renamed over it.

    Raises:
        FileExistsError: something is already at `destination`.
        OSError: the file could not be written there.
    """
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".partial"
    )
    partial = Path(name)
    try:
        try:
            # mkstemp makes the file private; the output gets the mode a new file would.
            os.fchmod(descriptor, 0o666 & ~_umask())
        except OSError as error:  # a FAT mount has no modes to set
            logger.info("could not set the mode of %s: %s", partial, error)
        finally:
            os.close(descriptor)
        on_partial(partial)
        shutil.copyfile(source, partial)
        try:
            os.link(partial, destination)
        except FileExistsError:
            raise
        except OSError as error:
            logger.info("could not link %s into place (%s); renaming it", destination, error)
            _rename_exclusively(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _umask() -> int:
    """The process umask, which can only be read by setting it (the worker is single
    threaded, so nothing creates a file in between)."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _rename_exclusively(partial: Path, destination: Path) -> None:
    os.close(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        partial.replace(destination)
    except OSError:
        destination.unlink(missing_ok=True)  # the empty file this call just created
        raise


def _failed(job: SynthesisJob, clock: Callable[[], float], reason: str, error: str) -> SynthesisJob:
    """A failure, with the contract reason it is recorded under; `stage` already says
    where the worker was."""
    return replace(job, state=FAILED, finished_at=clock(), error=error, reason=reason)


def _reachable(liveness: CheckLiveness) -> Callable[[], Awaitable[bool]]:
    async def reachable() -> bool:
        return await liveness() is not Liveness.ABSENT

    return reachable


async def _ready(
    liveness: CheckLiveness,
    *,
    monotonic: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    deadline = monotonic() + WARMUP_TIMEOUT_S
    while True:
        if await liveness() is Liveness.READY:
            return True
        if monotonic() >= deadline:
            return False
        await sleep(WARMUP_POLL_S)


async def run_job(
    state_dir: Path,
    job_id: str,
    *,
    speech: WorkerSpeech,
    encode: Encode = encode_audio,
    is_alive: Callable[[int], bool] = process_is_alive,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    within: Within = asyncio.wait_for,
    pid: int | None = None,
    record: Record = record_job,
) -> Job | None:
    jobs = JobStore(state_dir)
    job = jobs.read(job_id)
    if not isinstance(job, SynthesisJob):
        return None
    me = pid if pid is not None else os.getpid()
    lock = job_lock(state_dir, is_alive=is_alive, pid=me)
    try:
        lock.take_over(job.claimed_by)
    except JobBusy:
        # Another job took the lock between this job's start and this takeover: put it
        # back in the queue, in its place, for whoever ends that job to start again.
        jobs.write(replace(job, state=QUEUED))
        raise
    job = replace(job, worker_pid=me)
    jobs.write(job)
    registered = False
    try:
        if speech.supervisor is not None:
            await speech.supervisor.register()
            registered = True
        finished = await _synthesize(
            jobs,
            job,
            speech=speech,
            encode=encode,
            clock=clock,
            monotonic=monotonic,
            sleep=sleep,
            within=within,
        )
    except Exception as error:  # a bug here must still leave an honest record
        latest = jobs.read(job_id)  # how far it got is on the record
        base = latest if isinstance(latest, SynthesisJob) else job
        finished = replace(
            base, state=FAILED, finished_at=clock(), error=f"worker error: {error}", reason="other"
        )
        jobs.write(finished)
        record(finished)
        raise
    else:
        jobs.write(finished)
        record(finished)
    finally:
        jobs.discard_artifacts(job_id)
        try:
            if registered and speech.supervisor is not None:
                await speech.supervisor.release()
        finally:
            lock.release()  # whatever the supervisor does, the next job may start
    return finished


async def _synthesize(
    jobs: JobStore,
    job: SynthesisJob,
    *,
    speech: WorkerSpeech,
    encode: Encode,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    within: Within,
) -> SynthesisJob:
    try:
        text = jobs.text_path(job.job_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _failed(job, clock, "other", f"the staged text is unreadable: {error}")
    chunks = chunk_text(text, max_chars=CHUNK_CHARS)
    if not chunks:
        return _failed(
            replace(job, chunks=0), clock, "input_unsupported", "the text has no words to speak"
        )
    job = replace(job, chunks=len(chunks), stage="synthesize")
    jobs.write(job)

    state = await speech.liveness()
    job = replace(job, server_warm=state is Liveness.READY)
    if state is Liveness.ABSENT:
        if speech.supervisor is None:
            return _failed(
                job,
                clock,
                "server_unavailable",
                "the speech server is not running and NEMO_AUTOSTART=0",
            )
        try:
            await speech.supervisor.warm(_reachable(speech.liveness))
        except OSError as error:
            return _failed(
                job,
                clock,
                "nemo_missing",
                f"could not start the speech server ({error}); run /voicebox:setup",
            )
    if not await _ready(speech.liveness, monotonic=monotonic, sleep=sleep):
        return _failed(
            job,
            clock,
            "server_unavailable",
            f"the speech server did not become ready within {WARMUP_TIMEOUT_S:g} s",
        )

    work = jobs.work_dir(job.job_id)
    work.mkdir(parents=True, exist_ok=True)
    pieces: list[Piece] = []
    for index, chunk in enumerate(chunks):
        which = f"chunk {index + 1} of {len(chunks)}"
        timeout = request_timeout_s(len(chunk.text), job.slowest_chars_per_s)
        started = monotonic()
        try:
            audio = await within(
                speech.synthesizer.synthesize(chunk.text, voice=job.voice), timeout
            )
        except TimeoutError:
            message = f"the speech server did not answer within {timeout:.0f} s"
            return _failed(job, clock, "timeout", f"{which}: {message}")
        except SynthesisFailed as error:
            reason = REASON_OF_FAILURE.get(error.kind, "other")
            return _failed(job, clock, reason, f"{which}: {error}")
        seconds = monotonic() - started
        path = work / f"{index:06d}.wav"
        path.write_bytes(audio)
        pieces.append(Piece(path, chunk.paragraph_end))
        slowest = job.slowest_chars_per_s
        if len(chunk.text) >= PACE_MIN_CHARS:
            pace = len(chunk.text) / max(seconds, MIN_REQUEST_S)
            slowest = pace if slowest is None else min(slowest, pace)
        job = replace(
            job,
            chunks_done=index + 1,
            chars_done=job.chars_done + len(chunk.text),
            synth_ms=job.synth_ms + round(seconds * 1000),
            slowest_chars_per_s=slowest,
        )
        jobs.write(job)

    job = replace(job, stage="encode")
    jobs.write(job)
    started = monotonic()
    joined = work / "joined.wav"
    try:
        audio_ms = concatenate(
            pieces, joined, gap_ms=CHUNK_GAP_MS, paragraph_gap_ms=PARAGRAPH_GAP_MS
        )
    except FormatMismatchError as error:
        return _failed(job, clock, "format_mismatch", str(error))
    except OutputTooLargeError as error:
        return _failed(job, clock, "output_unwritable", f"{error}; split the text and retry")
    except (wave.Error, EOFError) as error:
        return _failed(
            job, clock, "model_failed", f"the speech server's audio is not a WAV: {error}"
        )
    encoded = joined
    if job.format != "wav":
        encoded = work / f"joined.{job.format}"
        try:
            await encode(joined, encoded, job.format)
        except ToolMissing as error:
            return _failed(job, clock, "ffmpeg_missing", str(error))
        except ConversionFailed as error:
            reason = "timeout" if error.timed_out else "conversion_failed"
            return _failed(job, clock, reason, str(error))
    encode_ms = round((monotonic() - started) * 1000)
    job = replace(job, stage="write", encode_ms=encode_ms, audio_ms=audio_ms)
    jobs.write(job)

    output = Path(job.output)
    size = encoded.stat().st_size

    def on_partial(partial: Path) -> None:
        jobs.write(replace(job, partial=str(partial)))

    try:
        place(encoded, output, on_partial=on_partial)
    except FileExistsError:
        return _failed(
            job,
            clock,
            "output_unwritable",
            f"{output} appeared while the job ran; voicebox never overwrites a file",
        )
    except OSError as error:
        return _failed(job, clock, "output_unwritable", f"could not write {output}: {error}")
    return replace(job, state=DONE, finished_at=clock(), output_bytes=size)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m voicebox.synthesize.worker <state_dir> <job_id>", file=sys.stderr)
        return 2
    # Nobody reads this process's output: it is detached and its stderr is the job log,
    # which job_status shows beside a failure.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    # An entry point, so it reads the environment it inherited from the session that
    # spawned it: the shared server settings, the API key included, never written to disk.
    server = ServerSettings.from_env(os.environ)
    speech = WorkerSpeech(
        synthesizer=NemoSynthesizer(
            base_url=server.url,
            api_key=server.api_key,
            model=server.tts_model,
            timeout=CLIENT_TIMEOUT_S,
        ),
        liveness=NemoTranscriber(base_url=server.url, api_key=server.api_key).liveness,
        supervisor=supervisor_for(server) if server.autostart else None,
    )
    try:
        job = asyncio.run(run_job(Path(args[0]), args[1], speech=speech))
    except Exception:
        logger.exception("the synthesis worker did not finish")
        return 1
    finally:
        start_next_job(Path(args[0]))
    return 0 if job is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
