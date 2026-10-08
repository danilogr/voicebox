"""The detached process that owns one transcription job from start to finish.

Run as `python -m voicebox.transcribe.worker <state_dir> <job_id>` in its own
session, so it outlives the MCP server that started it: a three-hour recording
is twenty-six minutes of work, and nothing should depend on that window staying
open. It is also what reports the outcome, because by the time a long job ends the
session that asked for it may be gone.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from speak_core.supervisor import process_is_alive

from voicebox.jobs.desk import Record, start_next_job
from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import DONE, EMPTY, FAILED, QUEUED, Job, JobStore, TranscriptionJob
from voicebox.programs import ConversionFailed, Runner, ToolMissing, run_convert_command
from voicebox.telemetry.jobs import record_job
from voicebox.transcribe.output import transcript_text
from voicebox.transcribe.prepare import convert

logger = logging.getLogger(__name__)

CliRunner = Callable[[Sequence[str], Path], Awaitable[int]]
NEMO_REMEDY = "run /voicebox:setup to install it"


async def run_cli(argv: Sequence[str], log: Path) -> int:
    with log.open("ab") as sink:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=sink,
        )
        return await process.wait()


def cli_argv(job: TranscriptionJob, audio: Path) -> list[str]:
    argv = [
        job.nemo_binary,
        "--quiet",
        "transcribe",
        str(audio),
        "--model",
        job.asr_model,
        "-f",
        job.format,
        "-o",
        job.output,
        "--force",
    ]
    if job.diarize:
        argv.append("--diarize")
    if job.language:
        argv += ["--language", job.language]
    if job.translate_to:
        argv += ["--translate-to", job.translate_to]
    return argv


async def run_job(
    state_dir: Path,
    job_id: str,
    *,
    run: Runner = run_convert_command,
    run_cli: CliRunner = run_cli,
    is_alive: Callable[[int], bool] = process_is_alive,
    clock: Callable[[], float] = time.time,
    pid: int | None = None,
    record: Record = record_job,
) -> Job | None:
    jobs = JobStore(state_dir)
    job = jobs.read(job_id)
    if not isinstance(job, TranscriptionJob):
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
    try:
        finished = await _transcribe(jobs, job, run=run, run_cli=run_cli, clock=clock)
    except Exception as error:  # a bug here must still leave an honest record
        finished = replace(
            job, state=FAILED, finished_at=clock(), error=f"worker error: {error}", reason="other"
        )
        jobs.write(finished)
        record(finished)
        raise
    else:
        jobs.write(finished)
        record(finished)
    finally:
        jobs.discard_artifacts(job_id)
        lock.release()
    return finished


async def _transcribe(
    jobs: JobStore,
    job: TranscriptionJob,
    *,
    run: Runner,
    run_cli: CliRunner,
    clock: Callable[[], float],
) -> TranscriptionJob:
    audio = Path(job.source)
    if job.convert:
        audio = jobs.wav_path(job.job_id)
        started = clock()
        try:
            await convert(Path(job.source), audio, run=run)
        except ToolMissing as error:
            return _failed(job, clock, "ffmpeg_missing", str(error))
        except ConversionFailed as error:
            reason = "timeout" if error.timed_out else "conversion_failed"
            return _failed(job, clock, reason, str(error))
        job = replace(job, convert_ms=int((clock() - started) * 1000), stage="asr")
        jobs.write(job)
    started = clock()
    try:
        code = await run_cli(cli_argv(job, audio), jobs.log_path(job.job_id))
    except FileNotFoundError:
        return _failed(job, clock, "nemo_missing", str(ToolMissing(job.nemo_binary, NEMO_REMEDY)))
    asr_ms = int((clock() - started) * 1000)
    output = Path(job.output)
    if code != 0 or not output.exists():
        message = jobs.log_tail(job.job_id) or f"{job.nemo_binary} exited with {code}"
        return _failed(replace(job, asr_ms=asr_ms), clock, "model_failed", message)
    text = transcript_text(output, job.format)
    return replace(
        job,
        state=DONE if text else EMPTY,
        finished_at=clock(),
        asr_ms=asr_ms,
        words=len(text.split()),
        chars=len(text),
    )


def _failed(
    job: TranscriptionJob, clock: Callable[[], float], reason: str, error: str
) -> TranscriptionJob:
    """A failure, with the contract reason it is recorded under; `stage` already says
    where the worker was."""
    return replace(job, state=FAILED, finished_at=clock(), error=error, reason=reason)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m voicebox.transcribe.worker <state_dir> <job_id>", file=sys.stderr)
        return 2
    # Nobody reads this process's output: it is detached, and its stderr is the job
    # log. Sending this module's own logging there too, and logging a traceback
    # before exiting, is what turns a worker that died into something readable -
    # `job_status` prints the tail of that log next to the failure.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        job = asyncio.run(run_job(Path(args[0]), args[1]))
    except Exception:
        logger.exception("the transcription worker did not finish")
        return 1
    finally:
        start_next_job(Path(args[0]))
    return 0 if job is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
