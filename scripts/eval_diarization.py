"""Score how well transcription labels speakers across a long recording, window by window.

NeMo-Speech.cpp's `transcribe --diarize` stops tagging speakers on everything older than
roughly the last fifteen minutes of a stream once it passes about twenty (issue #3). This
script runs a recording with known speaker turns through voicebox's transcription worker
and scores each word's speaker against the truth in five-minute windows, so that failure,
and anything that fixes it, can be seen and repeated.

Needs nemo-speech and ffmpeg; `ami` also needs the network. Not part of the test suite:
it runs the real models on real audio for several minutes.

    uv run python scripts/eval_diarization.py synthetic          # two MagpieTTS voices, 26 min
    uv run python scripts/eval_diarization.py ami                # AMI meeting IS1009c, 30 min
    uv run python scripts/eval_diarization.py score out.json truth.rttm

`--engine nemo` runs `nemo-speech transcribe --diarize` directly instead of the worker,
which shows the runtime's own behaviour. Exits 1 when any window scores below
`--min-accuracy`.
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from voicebox.jobs.lock import job_lock
from voicebox.jobs.store import DONE, RUNNING, JobStore, TranscriptionJob, new_job_id
from voicebox.transcribe.speakers import UNTAGGED
from voicebox.transcribe.worker import run_job

RATE = 16000
FRAME_S = 0.01  # resolution of the truth timeline

# Neutral, speaker-agnostic lines. Both voices read the same pool, so nothing in the
# words says who is talking: only the voice does.
LINES = (
    "I looked at the numbers again this morning and they still hold up.",
    "Can we move the review to Thursday, after the team sync?",
    "The vendor sent a new quote, and it is a little higher than last time.",
    "I think the simplest version is the one we should ship first.",
    "Let me share my screen so you can see the dashboard.",
    "We tried that in the spring and it did not move the metric much.",
    "The warehouse in Ohio is running two shifts until the end of the month.",
    "If the pilot goes well, they want to roll it out to every site.",
    "I am not sure the estimate includes the time for testing.",
    "Their security team asked for a written data retention policy.",
    "That sounds reasonable to me, as long as we keep it small.",
    "Who owns the follow up with the customer after this call?",
    "The model gets the easy cases right and struggles with blurry photos.",
    "We should write down the decision so nobody has to remember it.",
    "My flight lands late on Sunday, so Monday morning is tight.",
    "I would rather spend the week talking to users than polishing slides.",
    "The invoice was paid, but the receipt never reached the shared inbox.",
    "Could you send me the recording once the meeting is over?",
    "There are three open questions, and only one of them is urgent.",
    "Honestly, the hardest part is getting the data in the first place.",
    "Let us give it two weeks and look at the results together.",
    "The new hire starts on the first, and I will handle the onboarding.",
    "I like the idea, but I worry about how we would support it.",
    "Okay, I think that covers everything on my list for today.",
)

AMI_AUDIO = (
    "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/{m}.Mix-Headset.wav"
)
AMI_RTTM = (
    "https://raw.githubusercontent.com/pyannote/AMI-diarization-setup/main/"
    "only_words/rttms/test/{m}.rttm"
)


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str


# ---- Recordings ----


def synthetic(work: Path, minutes: float, voices: list[str], seed: int, binary: str) -> Path:
    """A conversation of alternating turns in two or more voices, and its truth as RTTM."""
    bank = {
        voice: [_line_clip(work, binary, voice, line, seed) for line in LINES] for voice in voices
    }
    rng = random.Random(seed)
    turns: list[Turn] = []
    frames = bytearray()
    speaker = 0
    while len(frames) < minutes * 60 * RATE * 2:
        voice = voices[speaker % len(voices)]
        for _ in range(rng.choice((1, 1, 2, 2, 3))):
            clip = rng.choice(bank[voice])
            start = len(frames) / 2 / RATE
            frames += clip
            turns.append(Turn(start, len(frames) / 2 / RATE, voice))
            frames += _silence(rng.uniform(0.15, 0.4))
        frames += _silence(rng.uniform(0.3, 0.9))
        speaker += 1
    audio = work / f"synthetic-{'-'.join(voices)}-{seed}-{minutes:g}min.wav"
    with wave.open(str(audio), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(frames))
    _write_rttm(audio.with_suffix(".rttm"), audio.stem, turns)
    return audio


def _line_clip(work: Path, binary: str, voice: str, line: str, seed: int) -> bytes:
    """One line in one voice as 16 kHz mono PCM, cached by voice, seed and text."""
    key = hashlib.sha1(f"{voice}|{seed}|{line}".encode()).hexdigest()[:12]
    clip = work / "lines" / f"{voice}-{key}.wav"
    if not clip.exists():
        clip.parent.mkdir(parents=True, exist_ok=True)
        raw = clip.with_suffix(".tts.wav")
        _run(
            [
                binary,
                "--quiet",
                "synthesize",
                line,
                "--voice",
                voice,
                "--seed",
                str(seed),
                "-o",
                str(raw),
                "--force",
            ]
        )
        _to_pcm16k(raw, clip)
        raw.unlink()
    with wave.open(str(clip), "rb") as read:
        return read.readframes(read.getnframes())


def ami(work: Path, meeting: str) -> Path:
    """An AMI meeting's headset mix and reference RTTM. AMI is CC BY 4.0 (University of
    Edinburgh); the RTTM is pyannote's AMI-diarization-setup, words only."""
    audio = work / f"{meeting}.wav"
    rttm = audio.with_suffix(".rttm")
    for url, dest in (
        (AMI_AUDIO.format(m=meeting), work / f"{meeting}.Mix-Headset.wav"),
        (AMI_RTTM.format(m=meeting), rttm),
    ):
        if not dest.exists():
            print(f"downloading {url}", file=sys.stderr)
            urllib.request.urlretrieve(url, dest)
    if not audio.exists():
        _to_pcm16k(work / f"{meeting}.Mix-Headset.wav", audio)
    return audio


def _silence(seconds: float) -> bytes:
    return bytes(2 * int(seconds * RATE))


def _to_pcm16k(source: Path, dest: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            str(RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL)


# ---- Transcribing ----


async def through_worker(audio: Path, output: Path, binary: str, model: str) -> None:
    """Transcribe as the `transcribe` tool would (json, speakers on), through the real
    worker, with a throwaway state directory so no real job or telemetry is touched."""
    with tempfile.TemporaryDirectory(prefix="voicebox-eval-") as tmp:
        await _through_worker(Path(tmp), audio, output, binary, model)


async def _through_worker(state: Path, audio: Path, output: Path, binary: str, model: str) -> None:
    me = os.getpid()
    with wave.open(str(audio), "rb") as read:
        duration = read.getnframes() / read.getframerate()
    job = TranscriptionJob(
        job_id=new_job_id(),
        state=RUNNING,
        source=str(audio),
        output=str(output),
        format="json",
        nemo_binary=binary,
        claimed_by=me,
        created_at=time.time(),
        stage="asr",
        diarize=True,
        language=None,
        translate_to=None,
        duration_s=duration,
        size_bytes=audio.stat().st_size,
        codec="pcm_s16le",
        convert=False,
        asr_model=model,
    )
    JobStore(state).write(job)
    job_lock(state, is_alive=lambda pid: pid == me, pid=me).acquire()
    finished = await run_job(
        state, job.job_id, is_alive=lambda pid: pid == me, pid=me, record=lambda _: None
    )
    if finished is None or finished.state != DONE:
        raise SystemExit(
            f"transcription did not finish: {finished.error if finished else 'no job'}"
        )


def through_nemo(audio: Path, output: Path, binary: str, model: str) -> None:
    _run(
        [
            binary,
            "--quiet",
            "transcribe",
            str(audio),
            "--model",
            model,
            "-f",
            "json",
            "-o",
            str(output),
            "--force",
            "--diarize",
        ]
    )


# ---- Scoring ----


def read_rttm(path: Path) -> list[Turn]:
    turns = []
    for line in path.read_text().splitlines():
        part = line.split()
        if part and part[0] == "SPEAKER":
            start, length = float(part[3]), float(part[4])
            turns.append(Turn(start, start + length, part[7]))
    return turns


def _write_rttm(path: Path, recording: str, turns: list[Turn]) -> None:
    path.write_text(
        "".join(
            f"SPEAKER {recording} 1 {t.start:.3f} {t.end - t.start:.3f} "
            f"<NA> <NA> {t.speaker} <NA> <NA>\n"
            for t in turns
        )
    )


@dataclass(frozen=True)
class Scored:
    window: int
    hyp: int
    ref: str


def score_words(words: list[dict], turns: list[Turn], window_s: float) -> list[Scored]:
    """Each word whose midpoint falls in exactly one reference turn, with its window.
    Words in silence or in overlapping speech have no single right answer and are skipped."""
    speakers = sorted({t.speaker for t in turns})
    end = max(t.end for t in turns)
    masks = [0] * (int(end / FRAME_S) + 2)
    for t in turns:
        bit = 1 << speakers.index(t.speaker)
        for frame in range(int(t.start / FRAME_S), int(t.end / FRAME_S) + 1):
            masks[frame] |= bit
    scored = []
    for word in words:
        middle = (word["start"] + word["end"]) / 2
        frame = int(middle / FRAME_S)
        mask = masks[frame] if frame < len(masks) else 0
        if mask and mask & (mask - 1) == 0:
            scored.append(
                Scored(
                    int(middle // window_s),
                    int(word.get("speaker", 0)),
                    speakers[mask.bit_length() - 1],
                )
            )
    return scored


def best_mapping(scored: list[Scored]) -> dict[int, str]:
    """The one-to-one hypothesis-to-reference mapping that gets the most words right.
    An untagged word (speaker 0) is no one's, so it is never mapped and always a miss."""
    pairs = Counter((s.hyp, s.ref) for s in scored)
    hyps = sorted({s.hyp for s in scored if s.hyp != UNTAGGED})
    refs = sorted({s.ref for s in scored})
    # Permute the longer list over the shorter one's length: every one-to-one mapping,
    # each once, however many speakers the diarizer splits a recording into.
    if len(hyps) <= len(refs):
        orders = ((hyps, order) for order in itertools.permutations(refs, len(hyps)))
    else:
        orders = ((chosen, refs) for chosen in itertools.permutations(hyps, len(refs)))
    best: dict[int, str] = {}
    best_hits = -1
    for some_hyps, some_refs in orders:
        mapping = dict(zip(some_hyps, some_refs, strict=True))
        hits = sum(pairs[pair] for pair in mapping.items())
        if hits > best_hits:
            best_hits, best = hits, mapping
    return best


def report(words: list[dict], turns: list[Turn], window_s: float, min_accuracy: float) -> bool:
    scored = score_words(words, turns, window_s)
    if not scored:
        print("FAIL: no word falls in a single reference speaker's turn")
        return False
    mapping = best_mapping(scored)
    tags = [w.get("speaker") for w in words]
    changes = [words[i]["start"] for i in range(1, len(words)) if tags[i] != tags[i - 1]]
    print(f"{'window':<13} {'words':>6} {'accuracy':>9}")
    windows: dict[int, list[Scored]] = {}
    for s in scored:
        windows.setdefault(s.window, []).append(s)
    # Windows too short to say much are left out, unless every window is that short.
    judged = min(50, max(len(inside) for inside in windows.values()))
    passed = True
    for window, inside in sorted(windows.items()):
        accuracy = sum(mapping.get(s.hyp) == s.ref for s in inside) / len(inside)
        low = len(inside) >= judged and accuracy < min_accuracy
        passed &= not low
        span = f"{_clock(window * window_s)}-{_clock((window + 1) * window_s)}"
        print(f"{span:<13} {len(inside):>6} {accuracy:>9.2f}{'  <' if low else ''}")
    overall = sum(mapping.get(s.hyp) == s.ref for s in scored) / len(scored)
    passed &= overall >= min_accuracy
    print(f"{'overall':<13} {len(scored):>6} {overall:>9.2f}")
    first = _clock(changes[0]) if changes else "never"
    speakers = len({t.speaker for t in turns})
    print(f"speaker changes: {len(changes)} (first at {first}); reference speakers: {speakers}")
    print(
        f"{'PASS' if passed else 'FAIL'}: overall and every window of {judged}+ words"
        f" at or above {min_accuracy:.2f}"
    )
    return passed


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


# ---- Entry point ----


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "voicebox-eval")
    parser.add_argument("--engine", choices=("voicebox", "nemo"), default="voicebox")
    parser.add_argument("--window", type=float, default=300.0, help="window length in seconds")
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    parser.add_argument("--binary", default=os.environ.get("NEMO_BIN", "nemo-speech"))
    parser.add_argument("--model", default=os.environ.get("NEMO_ASR_MODEL", "nemotron-3.5"))
    sources = parser.add_subparsers(dest="source", required=True)
    synth = sources.add_parser("synthetic", help="a conversation in MagpieTTS voices")
    synth.add_argument("--minutes", type=float, default=26.0)
    synth.add_argument("--voices", nargs="+", default=["John", "Sofia"])
    synth.add_argument("--seed", type=int, default=7)
    meeting = sources.add_parser("ami", help="an AMI meeting (downloads ~60 MB)")
    meeting.add_argument("--meeting", default="IS1009c")
    score = sources.add_parser("score", help="score an existing json transcript")
    score.add_argument("transcript", type=Path)
    score.add_argument("truth", type=Path)
    args = parser.parse_args()

    if args.source == "score":
        transcript, truth = args.transcript, args.truth
    else:
        args.work.mkdir(parents=True, exist_ok=True)
        if args.source == "synthetic":
            audio = synthetic(args.work, args.minutes, args.voices, args.seed, args.binary)
        else:
            audio = ami(args.work, args.meeting)
        truth = audio.with_suffix(".rttm")
        transcript = audio.with_suffix(f".{args.engine}.json")
        print(f"transcribing {audio} with {args.engine}", file=sys.stderr)
        started = time.monotonic()
        if args.engine == "voicebox":
            asyncio.run(through_worker(audio, transcript, args.binary, args.model))
        else:
            through_nemo(audio, transcript, args.binary, args.model)
        print(f"took {time.monotonic() - started:.0f} s; transcript {transcript}", file=sys.stderr)
    words = json.loads(transcript.read_text())["words"]
    return 0 if report(words, read_rttm(truth), args.window, args.min_accuracy) else 1


if __name__ == "__main__":
    raise SystemExit(main())
