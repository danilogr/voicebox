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

from voicebox.transcribe.commands import Runner, ToolMissing, run_convert_command
from voicebox.transcribe.jobs import DONE, EMPTY, FAILED, Job, JobStore
from voicebox.transcribe.lock import TranscriptionBusy, transcription_lock
from voicebox.transcribe.output import transcript_text
from voicebox.transcribe.prepare import ConversionFailed, convert

logger = logging.getLogger(__name__)

CliRunner = Callable[[Sequence[str], Path], Awaitable[int]]
# Called once with a job's final record, whatever ended it. Telemetry plugs in here.
Record = Callable[[Job], None]


def record_nothing(job: Job) -> None:
    """The default until telemetry records jobs: nothing to do."""


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


def cli_argv(job: Job, audio: Path) -> list[str]:
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
    record: Record = record_nothing,
) -> Job | None:
    jobs = JobStore(state_dir)
    job = jobs.read(job_id)
    if job is None:
        return None
    me = pid if pid is not None else os.getpid()
    lock = transcription_lock(state_dir, is_alive=is_alive, pid=me)
    try:
        lock.take_over(job.claimed_by)
    except TranscriptionBusy as error:
        # Refused the lock, so this job never runs. Saying so is the whole of the
        # worker's answer here: without a record it would look like a worker that
        # vanished, and the session would wait out an estimate for work nobody is doing.
        refused = replace(job, state=FAILED, finished_at=clock(), error=str(error))
        jobs.write(refused)
        record(refused)
        raise
    job = replace(job, worker_pid=me)
    jobs.write(job)
    try:
        finished = await _transcribe(jobs, job, run=run, run_cli=run_cli, clock=clock)
    except Exception as error:  # a bug here must still leave an honest record
        finished = replace(job, state=FAILED, finished_at=clock(), error=f"worker error: {error}")
        jobs.write(finished)
        record(finished)
        raise
    else:
        jobs.write(finished)
        record(finished)
    finally:
        jobs.wav_path(job_id).unlink(missing_ok=True)
        lock.release()
    return finished


async def _transcribe(
    jobs: JobStore, job: Job, *, run: Runner, run_cli: CliRunner, clock: Callable[[], float]
) -> Job:
    audio = Path(job.source)
    if job.convert:
        audio = jobs.wav_path(job.job_id)
        try:
            await convert(Path(job.source), audio, run=run)
        except (ToolMissing, ConversionFailed) as error:
            return replace(job, state=FAILED, finished_at=clock(), error=str(error))
    started = clock()
    try:
        code = await run_cli(cli_argv(job, audio), jobs.log_path(job.job_id))
    except FileNotFoundError:
        missing = ToolMissing(job.nemo_binary, NEMO_REMEDY)
        return replace(job, state=FAILED, finished_at=clock(), error=str(missing))
    asr_ms = int((clock() - started) * 1000)
    output = Path(job.output)
    if code != 0 or not output.exists():
        reason = jobs.log_tail(job.job_id) or f"{job.nemo_binary} exited with {code}"
        return replace(job, state=FAILED, finished_at=clock(), asr_ms=asr_ms, error=reason)
    text = transcript_text(output, job.format)
    return replace(
        job,
        state=DONE if text else EMPTY,
        finished_at=clock(),
        asr_ms=asr_ms,
        words=len(text.split()),
        chars=len(text),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m voicebox.transcribe.worker <state_dir> <job_id>", file=sys.stderr)
        return 2
    # Nobody reads this process's output: it is detached, and its stderr is the job
    # log. Sending this module's own logging there too, and logging a traceback
    # before exiting, is what turns a worker that died into something readable -
    # `transcription_status` prints the tail of that log next to the failure.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        job = asyncio.run(run_job(Path(args[0]), args[1]))
    except Exception:
        logger.exception("the transcription worker did not finish")
        return 1
    return 0 if job is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
