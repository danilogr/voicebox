"""The synthesis worker: chunks in order, one file out, every failure named and
recorded once, and nothing of the job left behind."""

import errno
import os
import stat
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from speak_core.engines.asr import Liveness
from speak_core.engines.nemo import SynthesisFailed

from synthesis_support import (
    CLAIMER,
    JOB_ID,
    WORKER,
    CountingSupervisor,
    FakeSynthesizer,
    Liveliness,
    staged,
    wav_bytes,
)
from voicebox.jobs.desk import Desk, reconcile
from voicebox.jobs.lock import JobBusy, job_lock
from voicebox.jobs.store import CRASHED, DONE, FAILED, JobStore
from voicebox.programs import ConversionFailed, ToolMissing
from voicebox.synthesize.audio import OutputTooLargeError
from voicebox.synthesize.limits import (
    CHUNK_CHARS,
    CPU_CHARS_PER_S,
    FIRST_REQUEST_CEILING_S,
    REQUEST_TIMEOUT_S,
    request_timeout_s,
)
from voicebox.synthesize.worker import WorkerSpeech, place, run_job

READY = Liveness.READY
ABSENT = Liveness.ABSENT
STARTING = Liveness.STARTING

THREE_PARAGRAPHS = "First paragraph here.\n\nSecond one is here.\n\nThird and last."


async def no_sleep(_: float) -> None:
    return None


def ticking(step: float = 1.0):
    now = [0.0]

    def clock() -> float:
        now[0] += step
        return now[0]

    return clock


async def work(
    state_dir: Path,
    *,
    synthesizer=None,
    states=(READY,),
    supervisor=None,
    encode=None,
    recorded=None,
):
    speech = WorkerSpeech(
        synthesizer=synthesizer or FakeSynthesizer(),
        liveness=Liveliness(states),
        supervisor=supervisor,
    )
    kwargs = {"encode": encode} if encode is not None else {}
    return await run_job(
        state_dir,
        JOB_ID,
        speech=speech,
        is_alive=lambda pid: pid in (CLAIMER, WORKER),
        clock=ticking(),
        monotonic=ticking(0.5),
        sleep=no_sleep,
        pid=WORKER,
        record=(recorded.append if recorded is not None else (lambda job: None)),
        **kwargs,
    )


async def test_every_chunk_is_spoken_in_order_into_one_wav(tmp_path):
    staged(tmp_path, THREE_PARAGRAPHS)
    synthesizer, recorded, supervisor = FakeSynthesizer(), [], CountingSupervisor()

    finished = await work(
        tmp_path, synthesizer=synthesizer, supervisor=supervisor, recorded=recorded
    )

    assert finished.state == DONE
    assert synthesizer.texts == ["First paragraph here.", "Second one is here.", "Third and last."]
    assert set(synthesizer.voices) == {"Sofia"}
    assert (finished.chunks, finished.chunks_done) == (3, 3)
    # three 100 ms chunks and two 700 ms paragraph pauses
    assert finished.audio_ms == 1700
    with wave.open(str(tmp_path / "out.wav")) as out:
        assert out.getnframes() == 22050 * 1700 // 1000
    assert finished.output_bytes == (tmp_path / "out.wav").stat().st_size
    assert finished.server_warm is True and finished.synth_ms > 0
    assert [job.state for job in recorded] == [DONE]


async def test_nothing_of_the_job_is_left_behind(tmp_path):
    staged(tmp_path, THREE_PARAGRAPHS)
    supervisor = CountingSupervisor()
    await work(tmp_path, supervisor=supervisor)
    jobs = JobStore(tmp_path)
    assert not jobs.text_path(JOB_ID).exists()
    assert not jobs.work_dir(JOB_ID).exists()
    assert job_lock(tmp_path, is_alive=lambda pid: True).owner() is None
    assert (supervisor.registered, supervisor.released) == (1, 1)
    assert not list(tmp_path.glob(".out.wav.partial"))


async def test_a_long_text_is_cut_into_chunks_that_fit(tmp_path):
    text = " ".join(["A sentence that keeps going on and on."] * 200)
    staged(tmp_path, text)
    synthesizer = FakeSynthesizer()
    await work(tmp_path, synthesizer=synthesizer)
    assert all(len(chunk) <= CHUNK_CHARS for chunk in synthesizer.texts)
    assert "".join("".join(synthesizer.texts).split()) == "".join(text.split())


async def test_a_failed_chunk_fails_the_job_naming_it(tmp_path):
    staged(tmp_path, THREE_PARAGRAPHS)
    finished = await work(tmp_path, synthesizer=FakeSynthesizer(fail_at=3))
    assert finished.state == FAILED
    assert (finished.reason, finished.stage, finished.chunks_done) == ("timeout", "synthesize", 2)
    assert finished.error.startswith("chunk 3 of 3:")
    assert not (tmp_path / "out.wav").exists()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (SynthesisFailed.UNREACHABLE, "server_unavailable"),
        (SynthesisFailed.REFUSED, "model_failed"),
        (SynthesisFailed.TIMEOUT, "timeout"),
    ],
)
async def test_each_kind_of_server_failure_has_its_reason(tmp_path, kind, reason):
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path, synthesizer=FakeSynthesizer(fail_at=1, kind=kind))
    assert finished.reason == reason


async def test_a_warm_server_is_not_started_again(tmp_path):
    staged(tmp_path, "One chunk.")
    supervisor = CountingSupervisor()
    await work(tmp_path, supervisor=supervisor, states=(READY,))
    assert supervisor.warmed == 0


async def test_an_absent_server_is_started_and_waited_for(tmp_path):
    staged(tmp_path, "One chunk.")
    supervisor = CountingSupervisor()
    finished = await work(tmp_path, supervisor=supervisor, states=(ABSENT, ABSENT, STARTING, READY))
    assert supervisor.warmed == 1
    assert finished.state == DONE and finished.server_warm is False


async def test_an_absent_server_without_autostart_fails_at_once(tmp_path):
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path, supervisor=None, states=(ABSENT,))
    assert (finished.state, finished.reason, finished.stage) == (
        FAILED,
        "server_unavailable",
        "synthesize",
    )


async def test_a_server_that_never_becomes_ready_fails(tmp_path):
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path, supervisor=CountingSupervisor(), states=(ABSENT, STARTING))
    assert (finished.reason, finished.stage) == ("server_unavailable", "synthesize")
    assert "did not become ready" in finished.error


async def test_a_missing_nemo_binary_is_named(tmp_path):
    staged(tmp_path, "One chunk.")
    supervisor = CountingSupervisor(warm_error=FileNotFoundError("nemo-speech"))
    finished = await work(tmp_path, supervisor=supervisor, states=(ABSENT,))
    assert finished.reason == "nemo_missing" and "/voicebox:setup" in finished.error


async def test_chunks_in_different_formats_fail_rather_than_corrupt(tmp_path):
    staged(tmp_path, THREE_PARAGRAPHS)

    class Drifting(FakeSynthesizer):
        async def synthesize(self, text, *, voice) -> bytes:
            await super().synthesize(text, voice=voice)
            return wav_bytes(rate=44100 if len(self.texts) == 2 else 22050)

    finished = await work(tmp_path, synthesizer=Drifting())
    assert (finished.reason, finished.stage) == ("format_mismatch", "encode")


async def test_audio_that_is_not_a_wav_is_the_models_failure(tmp_path):
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path, synthesizer=FakeSynthesizer(audio=b"not a wav"))
    assert (finished.reason, finished.stage) == ("model_failed", "encode")


async def test_m4a_is_encoded_from_the_joined_wav(tmp_path):
    staged(tmp_path, "One chunk.", format="m4a", output=str(tmp_path / "out.m4a"))
    calls = []

    async def encode(source, dest, fmt) -> None:
        calls.append((source.name, dest.name, fmt))
        dest.write_bytes(b"m4a bytes")

    finished = await work(tmp_path, encode=encode)
    assert calls == [("joined.wav", "joined.m4a", "m4a")]
    assert (tmp_path / "out.m4a").read_bytes() == b"m4a bytes"
    assert finished.state == DONE and finished.encode_ms is not None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ToolMissing("ffmpeg", "install it"), "ffmpeg_missing"),
        (ConversionFailed("Unknown encoder"), "conversion_failed"),
        (ConversionFailed("ffmpeg timed out", timed_out=True), "timeout"),
    ],
)
async def test_an_encoding_failure_has_its_reason(tmp_path, error, reason):
    staged(tmp_path, "One chunk.", format="mp3", output=str(tmp_path / "out.mp3"))

    async def encode(source, dest, fmt) -> None:
        raise error

    finished = await work(tmp_path, encode=encode)
    assert (finished.reason, finished.stage) == (reason, "encode")


async def test_an_output_directory_that_vanished_is_unwritable(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    staged(tmp_path, "One chunk.", output=str(gone / "out.wav"))
    gone.rmdir()
    finished = await work(tmp_path)
    assert (finished.reason, finished.stage) == ("output_unwritable", "write")


async def test_a_worker_that_loses_the_lock_race_is_refused_busy(tmp_path):
    staged(tmp_path, "One chunk.")
    job_lock(tmp_path, is_alive=lambda pid: True, pid=300).take_over(CLAIMER)
    recorded = []
    with pytest.raises(JobBusy):
        await run_job(
            tmp_path,
            JOB_ID,
            speech=WorkerSpeech(FakeSynthesizer(), Liveliness((READY,)), None),
            is_alive=lambda pid: True,
            pid=WORKER,
            record=recorded.append,
        )
    assert [(job.state, job.reason) for job in recorded] == [(FAILED, "busy")]


def test_place_never_leaves_half_a_file(tmp_path):
    source = tmp_path / "joined.wav"
    source.write_bytes(b"audio")
    place(source, tmp_path / "out.wav")
    assert (tmp_path / "out.wav").read_bytes() == b"audio"
    assert not list(tmp_path.glob(".*.partial"))


def test_a_missing_record_is_nothing_to_do(tmp_path):
    import asyncio

    speech = WorkerSpeech(FakeSynthesizer(), Liveliness((READY,)), None)
    assert asyncio.run(run_job(tmp_path, JOB_ID, speech=speech)) is None


# A request's timeout follows the pace this job's server has shown on full-sized
# requests, bounded by the CPU backend's (rulings Q39, Q41): a slow machine finishes, a
# server that stops answering fails the job after one request, and neither a heading's
# short request nor a wait behind another session stretches later timeouts.


class PacedServer(FakeSynthesizer):
    """A speech server on a fake clock: a request takes `overhead + len(text) / rate`
    seconds, plus `queued[n]` for request number n, and request `hang_at` never answers."""

    def __init__(
        self,
        rate: float,
        *,
        overhead: float = 0.0,
        queued: dict[int, float] | None = None,
        hang_at: int | None = None,
    ) -> None:
        super().__init__()
        self.now = 0.0
        self.timeouts: list[float] = []
        self._rate = rate
        self._overhead = overhead
        self._queued = queued or {}
        self._hang_at = hang_at

    async def synthesize(self, text, *, voice) -> bytes:
        audio = await super().synthesize(text, voice=voice)
        n = len(self.texts)
        if n == self._hang_at:
            self.now += 1e9
        else:
            self.now += self._queued.get(n, 0.0) + self._overhead + len(text) / self._rate
        return audio

    def monotonic(self) -> float:
        return self.now

    async def within(self, request, timeout: float) -> bytes:
        self.timeouts.append(timeout)
        started = self.now
        audio = await request
        if self.now - started > timeout:
            raise TimeoutError
        return audio


async def paced(state_dir: Path, server: PacedServer):
    return await run_job(
        state_dir,
        JOB_ID,
        speech=WorkerSpeech(server, Liveliness((READY,)), None),
        is_alive=lambda pid: pid in (CLAIMER, WORKER),
        monotonic=server.monotonic,
        within=server.within,
        sleep=no_sleep,
        pid=WORKER,
        record=lambda job: None,
    )


# A paragraph of 1,364 characters: one full chunk.
FULL = " ".join(["This sentence is here to fill the chunk up."] * 31)
LONG = "\n\n".join([FULL] * 3)
WITH_HEADINGS = "\n\n".join(["Intro", FULL, "Next", FULL, "End", FULL])


async def test_a_cpu_only_machine_finishes_its_full_chunks(tmp_path):
    staged(tmp_path, LONG)
    server = PacedServer(rate=1.2)  # about 0.07x realtime: 19 minutes a chunk
    finished = await paced(tmp_path, server)
    assert finished.state == DONE and finished.chunks_done == 3
    assert server.timeouts == [request_timeout_s(len(FULL), None)] + 2 * [
        request_timeout_s(len(FULL), 1.2)
    ]
    assert all(len(FULL) / 1.2 < timeout for timeout in server.timeouts)
    assert finished.slowest_chars_per_s == pytest.approx(1.2)


async def test_the_slowest_full_request_sets_later_timeouts(tmp_path):
    staged(tmp_path, LONG)

    class Slowing(PacedServer):
        async def synthesize(self, text, *, voice) -> bytes:
            self._rate = 10.0 if len(self.texts) == 1 else 30.0
            return await super().synthesize(text, voice=voice)

    server = Slowing(rate=30.0)
    finished = await paced(tmp_path, server)
    assert finished.slowest_chars_per_s == pytest.approx(10.0)
    assert server.timeouts == [
        request_timeout_s(len(FULL), None),
        request_timeout_s(len(FULL), 30.0),
        request_timeout_s(len(FULL), 10.0),
    ]
    assert server.timeouts[2] == pytest.approx(4 * len(FULL) / 10.0)


async def test_short_requests_do_not_skew_the_pace(tmp_path):
    staged(tmp_path, WITH_HEADINGS)
    # each request costs 0.7 s before its text: a 6-character heading runs at ~8 chars/s
    server = PacedServer(rate=45.0, overhead=0.7)
    finished = await paced(tmp_path, server)
    full_pace = len(FULL) / (0.7 + len(FULL) / 45.0)
    assert finished.slowest_chars_per_s == pytest.approx(full_pace)
    assert server.timeouts[-1] == REQUEST_TIMEOUT_S  # 4 x 31 s is under the floor
    # the estimate counts every character over all the time spent: about 43 chars/s,
    # where the headings' own pace (~8 chars/s) would make it five times longer
    assert finished.eta_s < finished.chars / 40


async def test_a_short_first_request_stuck_behind_other_work_does_not_skew_the_pace(tmp_path):
    staged(tmp_path, WITH_HEADINGS)
    server = PacedServer(rate=45.0, queued={1: 19.9})  # "Intro." at about 0.3 chars/s
    finished = await paced(tmp_path, server)
    assert finished.state == DONE
    assert finished.slowest_chars_per_s == pytest.approx(45.0)
    assert server.timeouts[-1] == REQUEST_TIMEOUT_S


async def test_a_full_request_stuck_behind_other_work_is_bounded_by_the_cpu_pace(tmp_path):
    staged(tmp_path, LONG)
    server = PacedServer(rate=45.0, queued={1: 2000.0})  # about 0.68 chars/s
    finished = await paced(tmp_path, server)
    assert finished.state == DONE
    assert server.timeouts[1:] == 2 * [
        min(FIRST_REQUEST_CEILING_S, 4 * len(FULL) / CPU_CHARS_PER_S)
    ]


async def test_a_server_that_stops_answering_fails_the_job_after_one_request(tmp_path):
    staged(tmp_path, THREE_PARAGRAPHS)
    server = PacedServer(rate=29.5, hang_at=2)
    finished = await paced(tmp_path, server)
    assert (finished.state, finished.reason, finished.stage) == (FAILED, "timeout", "synthesize")
    assert finished.error.startswith("chunk 2 of 3:")
    assert len(server.texts) == 2  # no retry
    # only the request that succeeded is counted
    assert finished.synth_ms == round(len(server.texts[0]) / 29.5 * 1000)
    assert finished.chars_done == len(server.texts[0])
    assert not (tmp_path / "out.wav").exists()


async def test_the_partial_file_is_on_the_record_while_it_is_written(tmp_path, monkeypatch):
    staged(tmp_path, "One chunk.")
    seen = []
    link = os.link

    def spying(src, dst) -> None:
        job = JobStore(tmp_path).read(JOB_ID)
        seen.append((job.stage, job.partial, str(src)))
        link(src, dst)

    monkeypatch.setattr("voicebox.synthesize.worker.os.link", spying)
    finished = await work(tmp_path)
    ((stage, partial, src),) = seen
    assert stage == "write" and partial == src
    assert Path(partial).parent == tmp_path and Path(partial).name.startswith(".out.wav.")
    assert partial.endswith(".partial") and not Path(partial).exists()
    assert finished.state == DONE and finished.partial is None


async def test_the_output_gets_the_usual_permissions(tmp_path):
    staged(tmp_path, "One chunk.")
    await work(tmp_path)
    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE((tmp_path / "out.wav").stat().st_mode) == 0o666 & ~umask


async def test_a_filesystem_that_refuses_a_mode_still_gets_the_file(tmp_path, monkeypatch):
    def refused(descriptor, mode) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("voicebox.synthesize.worker.os.fchmod", refused)
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path)
    assert finished.state == DONE and (tmp_path / "out.wav").exists()


def crashed_while_writing(tmp_path: Path, partial: Path):
    """Reconcile a job whose worker died at stage write, with `partial` on its record."""
    job = staged(tmp_path, "One chunk.", worker_pid=9999)
    jobs = JobStore(tmp_path)
    jobs.write(replace(job, stage="write", chunks=1, chunks_done=1, partial=str(partial)))
    desk = Desk(
        jobs=jobs,
        lock=lambda: job_lock(tmp_path, is_alive=lambda p: False, pid=CLAIMER),
        is_alive=lambda p: False,
        spawn=None,  # type: ignore[arg-type]
        probe=None,  # type: ignore[arg-type]
        pid=CLAIMER,
    )
    assert reconcile(desk, jobs.read(JOB_ID)).state == CRASHED
    return jobs.read(JOB_ID)


def test_a_synthesis_that_crashed_while_writing_leaves_no_partial_file(tmp_path):
    partial = tmp_path / ".out.wav.abc123.partial"
    partial.write_bytes(b"half")
    record = crashed_while_writing(tmp_path, partial)
    assert not partial.exists() and record.partial is None


@pytest.mark.parametrize(
    "where",
    ["elsewhere/.out.wav.abc123.partial", "notes.txt", ".other.wav.abc123.partial", ".out.wav"],
)
def test_a_recorded_partial_that_is_not_ours_is_never_deleted(tmp_path, where):
    path = tmp_path / where
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"theirs")
    record = crashed_while_writing(tmp_path, path)
    assert path.read_bytes() == b"theirs" and record.partial is None


async def test_a_file_that_appeared_at_the_output_is_never_replaced(tmp_path):
    staged(tmp_path, "One chunk.")
    (tmp_path / "out.wav").write_bytes(b"mine")
    finished = await work(tmp_path)
    assert (finished.state, finished.reason, finished.stage) == (
        FAILED,
        "output_unwritable",
        "write",
    )
    assert (tmp_path / "out.wav").read_bytes() == b"mine"
    assert not list(tmp_path.glob(".*.partial"))


def test_place_refuses_a_destination_that_exists(tmp_path):
    source = tmp_path / "joined.wav"
    source.write_bytes(b"audio")
    (tmp_path / "out.wav").write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        place(source, tmp_path / "out.wav")
    assert (tmp_path / "out.wav").read_bytes() == b"mine"
    assert not list(tmp_path.glob(".*.partial"))


def test_place_works_where_hard_links_do_not(tmp_path, monkeypatch):
    def no_links(src, dst) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr("voicebox.synthesize.worker.os.link", no_links)
    source = tmp_path / "joined.wav"
    source.write_bytes(b"audio")
    place(source, tmp_path / "out.wav")
    assert (tmp_path / "out.wav").read_bytes() == b"audio"
    with pytest.raises(FileExistsError):
        place(source, tmp_path / "out.wav")
    assert not list(tmp_path.glob(".*.partial"))


@pytest.mark.parametrize("audio", [b"not a wav", wav_bytes()[:20]], ids=["garbage", "truncated"])
async def test_a_reply_that_is_not_a_whole_wav_is_the_models_failure(tmp_path, audio):
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path, synthesizer=FakeSynthesizer(audio=audio))
    assert (finished.state, finished.reason) == (FAILED, "model_failed")


async def test_audio_too_long_for_one_wav_is_a_named_failure(tmp_path, monkeypatch):
    def too_large(*args: object, **kwargs: object) -> None:
        raise OutputTooLargeError("5000000000 bytes of audio do not fit one WAV file")

    monkeypatch.setattr("voicebox.synthesize.worker.concatenate", too_large)
    staged(tmp_path, "One chunk.")
    finished = await work(tmp_path)
    assert (finished.state, finished.reason, finished.stage) == (
        FAILED,
        "output_unwritable",
        "encode",
    )
    assert "do not fit" in finished.error


async def test_a_text_with_no_words_fails_rather_than_writing_silence(tmp_path):
    staged(tmp_path, "... --- !!!")
    finished = await work(tmp_path)
    assert (finished.state, finished.reason, finished.stage) == (
        FAILED,
        "input_unsupported",
        "split",
    )
    assert finished.chunks == 0
    assert not (tmp_path / "out.wav").exists()


async def test_staged_text_that_is_gone_fails_the_job(tmp_path):
    staged(tmp_path, "One chunk.")
    JobStore(tmp_path).text_path(JOB_ID).unlink()
    recorded = []
    finished = await work(tmp_path, recorded=recorded)
    assert (finished.state, finished.reason, finished.stage) == (FAILED, "other", "split")
    assert [job.state for job in recorded] == [FAILED]


async def test_the_lock_is_freed_even_when_leaving_the_server_fails(tmp_path):
    class Failing(CountingSupervisor):
        async def release(self) -> bool:
            await super().release()
            raise PermissionError("clients directory")

    staged(tmp_path, "One chunk.")
    with pytest.raises(PermissionError):
        await work(tmp_path, supervisor=Failing())
    assert job_lock(tmp_path, is_alive=lambda pid: True).owner() is None
