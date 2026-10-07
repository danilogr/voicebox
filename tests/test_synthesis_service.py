"""synthesize at call time: every refusal named before any work starts, then the text
staged and the worker started with the lock held for it."""

import sys
from pathlib import Path

import pytest
from speak_core.engines.asr import Liveness

from synthesis_support import ME, OTHER, desk_at, request, settings, speech
from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import RUNNING, JobStore, SynthesisJob
from voicebox.synthesize.service import start


def source(tmp_path: Path, name: str = "notes.md", text: str = "# Notes\n\nHello there.") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


async def reply(tmp_path, req, *, sp=None, spawned=None, alive=(ME,), **server: object) -> str:
    return await start(
        desk_at(tmp_path, spawned, alive), sp or speech(), settings(tmp_path, **server), req
    )


def the_job(tmp_path: Path) -> SynthesisJob:
    (job,) = JobStore(tmp_path / "state").recent()
    assert isinstance(job, SynthesisJob)
    return job


async def test_a_markdown_file_is_staged_as_its_prose_and_the_worker_started(tmp_path):
    spawned: list = []
    md = source(tmp_path, text="# Notes\n\nSay **this**.\n\n```py\nnot this\n```\n\nAnd this.")
    answer = await reply(tmp_path, request(path=str(md)), spawned=spawned)
    job = the_job(tmp_path)
    assert answer.startswith("Writing notes.md") and job.job_id in answer and "job_status" in answer
    assert (job.state, job.stage, job.input_kind, job.input_format) == (
        RUNNING,
        "split",
        "file",
        "markdown",
    )
    assert job.output == str(tmp_path / "notes.wav")
    assert (job.voice, job.tts_model, job.paragraphs) == ("John", "magpie", 3)
    staged = JobStore(tmp_path / "state").text_path(job.job_id).read_text()
    assert staged == "Notes\n\nSay this.\n\nAnd this."
    assert spawned[0][-3:] == ["voicebox.synthesize.worker", str(tmp_path / "state"), job.job_id]
    assert spawned[0][0] == sys.executable
    assert job_lock(tmp_path / "state", is_alive=lambda p: True).owner() == ME


async def test_pasted_text_needs_an_output_and_goes_exactly_there(tmp_path):
    assert "give output" in await reply(tmp_path, request(text="Hello."))
    out = tmp_path / "hello.mp3"
    await reply(tmp_path, request(text="Hello there.", output=str(out), format="mp3"))
    job = the_job(tmp_path)
    assert (job.output, job.format, job.source, job.input_kind) == (str(out), "mp3", "", "text")


async def test_pasted_markdown_is_recognised(tmp_path):
    await reply(tmp_path, request(text="## Title\n\nBody.", output=str(tmp_path / "o.wav")))
    assert the_job(tmp_path).input_format == "markdown"


@pytest.mark.parametrize(
    "fields",
    [dict(), dict(text="Hi.", path="/tmp/x.md")],
)
async def test_exactly_one_of_text_or_path(tmp_path, fields):
    assert "exactly one of text or path" in await reply(tmp_path, request(**fields))


async def test_a_relative_path_is_refused(tmp_path):
    assert "must be absolute" in await reply(tmp_path, request(path="notes.md"))


async def test_a_missing_file_is_named(tmp_path):
    assert "no such file" in await reply(tmp_path, request(path=str(tmp_path / "gone.md")))


async def test_a_binary_or_non_utf8_file_is_not_text(tmp_path):
    binary = tmp_path / "a.txt"
    binary.write_bytes(b"\x00\x01\x02")
    latin = tmp_path / "b.txt"
    latin.write_bytes("caf\xe9".encode("latin-1"))
    assert "not a text file" in await reply(tmp_path, request(path=str(binary)))
    assert "not UTF-8" in await reply(tmp_path, request(path=str(latin)))


async def test_a_text_that_is_empty_once_markup_is_gone_is_refused(tmp_path):
    md = source(tmp_path, text="```\ncode only\n```\n")
    assert "nothing to say" in await reply(tmp_path, request(path=str(md)))


async def test_an_existing_output_is_never_overwritten(tmp_path):
    out = tmp_path / "taken.wav"
    out.write_bytes(b"precious")
    answer = await reply(tmp_path, request(text="Hello.", output=str(out)))
    assert "already exists" in answer and out.read_bytes() == b"precious"


async def test_the_default_output_steps_around_an_existing_file(tmp_path):
    md = source(tmp_path)
    (tmp_path / "notes.wav").write_bytes(b"older")
    await reply(tmp_path, request(path=str(md)))
    job = the_job(tmp_path)
    assert job.output == str(tmp_path / f"notes.{job.job_id[:8]}.wav")


@pytest.mark.parametrize(
    ("output", "fmt", "says"),
    [
        ("relative.wav", "wav", "absolute"),
        ("/{tmp}/voice.mp3", "wav", "ends in .mp3"),
        ("/{tmp}/missing-dir/voice.wav", "wav", "cannot write"),
    ],
)
async def test_an_output_that_cannot_be_right_is_refused(tmp_path, output, fmt, says):
    answer = await reply(
        tmp_path,
        request(text="Hello.", output=output.format(tmp=str(tmp_path).lstrip("/")), format=fmt),
    )
    assert says in answer


async def test_m4a_and_mp3_need_ffmpeg_but_wav_does_not(tmp_path):
    out = str(tmp_path / "o.m4a")
    no_ffmpeg = speech(have=("nemo-speech",))
    assert "needs ffmpeg" in await reply(
        tmp_path, request(text="Hi.", output=out, format="m4a"), sp=no_ffmpeg
    )
    wav = str(tmp_path / "o.wav")
    assert (await reply(tmp_path, request(text="Hi.", output=wav), sp=no_ffmpeg)).startswith(
        "Writing"
    )


async def test_without_nemo_the_remedy_is_setup(tmp_path):
    nothing = speech(state=Liveness.ABSENT, have=("ffmpeg",))
    answer = await reply(tmp_path, request(text="Hi.", output=str(tmp_path / "o.wav")), sp=nothing)
    assert "/voicebox:setup" in answer


async def test_a_stopped_server_the_user_runs_is_not_started(tmp_path):
    down = speech(state=Liveness.ABSENT)
    answer = await reply(
        tmp_path, request(text="Hi.", output=str(tmp_path / "o.wav")), sp=down, autostart=False
    )
    assert "NEMO_AUTOSTART" in answer


async def test_a_stopped_server_voicebox_may_start_is_a_cold_start(tmp_path):
    await reply(
        tmp_path, request(text="Hi.", output=str(tmp_path / "o.wav")), sp=speech(Liveness.ABSENT)
    )
    assert the_job(tmp_path).cold_start is True


async def test_the_voice_is_checked_and_spelled_as_the_server_spells_it(tmp_path):
    out = str(tmp_path / "o.wav")
    assert 'unknown voice "Nobody"' in await reply(
        tmp_path, request(text="Hi.", output=out, voice="Nobody")
    )
    await reply(tmp_path, request(text="Hi.", output=out, voice="sofia"))
    assert the_job(tmp_path).voice == "Sofia"


async def test_a_voice_is_taken_on_trust_while_the_server_is_down(tmp_path):
    await reply(
        tmp_path,
        request(text="Hi.", output=str(tmp_path / "o.wav"), voice="Aria"),
        sp=speech(Liveness.ABSENT),
    )
    assert the_job(tmp_path).voice == "Aria"


async def test_a_running_job_of_any_kind_makes_it_busy(tmp_path):
    job_lock(tmp_path / "state", is_alive=lambda p: True, pid=OTHER).acquire()
    answer = await reply(
        tmp_path, request(text="Hi.", output=str(tmp_path / "o.wav")), alive=(ME, OTHER)
    )
    assert "one job at a time" in answer
    assert JobStore(tmp_path / "state").recent() == []


async def test_a_dangling_symlink_at_the_output_is_refused_not_followed(tmp_path):
    target = tmp_path / "elsewhere.wav"
    link = tmp_path / "out.wav"
    link.symlink_to(target)
    answer = await reply(tmp_path, request(text="Hello.", output=str(link)))
    assert "already exists" in answer and not target.exists()
    assert JobStore(tmp_path / "state").recent() == []


async def test_a_dangling_symlink_at_the_default_output_is_stepped_around(tmp_path):
    md = source(tmp_path)
    (tmp_path / "notes.wav").symlink_to(tmp_path / "nowhere.wav")
    await reply(tmp_path, request(path=str(md)))
    job = the_job(tmp_path)
    assert job.output == str(tmp_path / f"notes.{job.job_id[:8]}.wav")


async def test_a_taken_fallback_name_is_refused(tmp_path, monkeypatch):
    import voicebox.synthesize.service as service

    md = source(tmp_path)
    (tmp_path / "notes.wav").write_bytes(b"a")
    monkeypatch.setattr(service, "new_job_id", lambda: "deadbeef-0000-4000-8000-000000000000")
    (tmp_path / "notes.deadbeef.wav").write_bytes(b"b")
    answer = await reply(tmp_path, request(path=str(md)))
    assert "give output" in answer and JobStore(tmp_path / "state").recent() == []


async def test_a_home_that_cannot_be_found_is_a_refusal_not_a_crash(tmp_path):
    assert "cannot resolve" in await reply(tmp_path, request(path="~nosuchuser_xyz/a.md"))
    answer = await reply(tmp_path, request(text="Hi.", output="~nosuchuser_xyz/a.wav"))
    assert "cannot resolve" in answer


async def test_an_unwritable_source_directory_points_at_output(tmp_path):
    md = source(tmp_path)
    tmp_path.chmod(0o500)
    try:
        answer = await reply(tmp_path, request(path=str(md)))
    finally:
        tmp_path.chmod(0o700)
    assert "give output" in answer
