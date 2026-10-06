"""The detached process that owns one transcription job from start to finish.

Run as `python -m speak.transcribe.worker <state_dir> <job_id>` in its own
session, so it outlives the MCP server that started it: a three-hour recording
is twenty-six minutes of work, and nothing should depend on that window staying
open. It is also what writes the telemetry row, because by the time a long job
ends the session that asked for it may be gone.
"""

import asyncio
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from speak_core.supervisor import process_is_alive
from speak_core.telemetry import store as telemetry_store

from speak.telemetry.contract import SCHEMA
from speak.telemetry.session import TranscriptionOutcome, clamped, record_transcription
from speak.transcribe.commands import Runner, ToolMissing, run_convert_command
from speak.transcribe.jobs import DONE, EMPTY, FAILED, Job, JobStore
from speak.transcribe.lock import TranscriptionBusy, transcription_lock
from speak.transcribe.output import transcript_text
from speak.transcribe.prepare import ConversionFailed, convert

logger = logging.getLogger(__name__)

CliRunner = Callable[[Sequence[str], Path], Awaitable[int]]
Record = Callable[[Job, TranscriptionOutcome], None]

NEMO_REMEDY = "run /speak:setup to install it"


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


def outcome_for(job: Job, *, status: str | None = None) -> TranscriptionOutcome:
    """What one job looked like, clamped to what the contract will accept.

    Nothing upstream bounds a file: a three-hour 4K video is a real thing to ask for
    a transcript of, and the row describing it has to stay shippable. `status`
    overrides the one the job's state implies, for an outcome the state cannot
    express.
    """
    if status is None:
        status = {DONE: "ok", EMPTY: "heard_nothing"}.get(job.state, "failed")
    return clamped(
        TranscriptionOutcome(
            status=status,
            source_bytes=job.size_bytes,
            audio_codec=job.codec,
            audio_ms=int(job.duration_s * 1000),
            asr_ms=job.asr_ms,
            converted=job.convert,
            diarize=job.diarize,
            format=job.format,
            words=job.words,
            chars=job.chars,
        )
    )


def write_telemetry(job: Job, outcome: TranscriptionOutcome) -> None:
    """Record the job for the session that asked, if that session records at all."""
    if job.telemetry is None:
        return
    try:
        conn = telemetry_store.connect(Path(job.telemetry["db_path"]), SCHEMA)
    except (OSError, sqlite3.Error) as error:
        logger.debug("telemetry: could not open the local store: %s", error)
        return
    try:
        record_transcription(
            conn,
            session_id=job.telemetry["session_id"],
            install_id=job.telemetry["install_id"],
            engine="nemo",
            model=job.asr_model,
            outcome=outcome,
        )
    finally:
        conn.close()


async def run_job(
    state_dir: Path,
    job_id: str,
    *,
    run: Runner = run_convert_command,
    run_cli: CliRunner = run_cli,
    is_alive: Callable[[int], bool] = process_is_alive,
    clock: Callable[[], float] = time.time,
    pid: int | None = None,
    record: Record = write_telemetry,
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
        # The job itself failed, but to telemetry it was refused, the same as a
        # `transcribe` call that loses the race in the session.
        refused = replace(job, state=FAILED, finished_at=clock(), error=str(error))
        jobs.write(refused)
        record(refused, outcome_for(refused, status="refused"))
        raise
    job = replace(job, worker_pid=me)
    jobs.write(job)
    try:
        finished = await _transcribe(jobs, job, run=run, run_cli=run_cli, clock=clock)
    except Exception as error:  # a bug here must still leave an honest record
        finished = replace(job, state=FAILED, finished_at=clock(), error=f"worker error: {error}")
        jobs.write(finished)
        record(finished, outcome_for(finished))
        raise
    else:
        jobs.write(finished)
        record(finished, outcome_for(finished))
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
        print("usage: python -m speak.transcribe.worker <state_dir> <job_id>", file=sys.stderr)
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
