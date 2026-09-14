#!/usr/bin/env python3
"""Record a dataset for fine-tuning speech recognition to one voice.

Every sentence in sentences.txt is read aloud several times, in passes: each sentence
once, then each sentence again. Takes read back to back copy each other; passes spread
over separate sittings capture how a voice actually varies. See README.md beside this file.

The dataset folder is self-contained - audio plus NeMo manifests with paths relative to
it - so it can be copied as-is to the machine that trains.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
SENTENCES = HERE / "sentences.txt"
DEFAULT_OUT = Path.home() / "speak-voice-dataset"
DEFAULT_TAKES = 3
SAMPLE_RATE = 16_000  # what the Nemotron and Parakeet recognisers expect
HOLD_OUT_EVERY = 10
WAV_HEADER_BYTES = 44
TRAIN_MANIFEST = "manifest_train.jsonl"
VAL_MANIFEST = "manifest_val.jsonl"
DEVICE_LINE = re.compile(r"\] \[\d+\] (.+)$")


def read_sentences(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def take_path(out: Path, sentence: int, take: int) -> Path:
    return out / "audio" / f"{sentence:03d}-{take}.wav"


def pending(out: Path, count: int, takes: int) -> Iterator[tuple[int, int]]:
    """Unrecorded (sentence, take) pairs, in pass order: every first take before any second."""
    for take in range(1, takes + 1):
        for sentence in range(1, count + 1):
            if not take_path(out, sentence, take).exists():
                yield sentence, take


def is_held_out(sentence: int) -> bool:
    """Held out by sentence, never by file.

    Every take of a held-out sentence stays out of training. Splitting takes of one
    sentence across both sets would let the model hear that exact sentence in this voice
    before being scored on it, and the word error rate would flatter the result.
    """
    return sentence % HOLD_OUT_EVERY == 0


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return round(audio.getnframes() / audio.getframerate(), 3)


def build_manifests(out: Path, sentences: Sequence[str]) -> tuple[int, int]:
    """Write NeMo manifests for every recorded take. Returns (train, held out) counts.

    Paths are relative to `out`: NeMo resolves a relative audio_filepath against the
    manifest's own directory, which is what lets the folder move between machines.
    """
    train: list[str] = []
    held_out: list[str] = []
    for path in sorted((out / "audio").glob("*.wav")):
        number, _, take = path.stem.partition("-")
        if not (number.isdigit() and take.isdigit()) or not 1 <= int(number) <= len(sentences):
            continue
        try:
            seconds = duration(path)
        except (wave.Error, EOFError):
            print(f"skipping unreadable take {path.name}")
            continue
        row = json.dumps(
            {
                "audio_filepath": path.relative_to(out).as_posix(),
                "duration": seconds,
                "text": sentences[int(number) - 1],
            }
        )
        (held_out if is_held_out(int(number)) else train).append(row)
    (out / TRAIN_MANIFEST).write_text("".join(f"{r}\n" for r in train), encoding="utf-8")
    (out / VAL_MANIFEST).write_text("".join(f"{r}\n" for r in held_out), encoding="utf-8")
    return len(train), len(held_out)


def resolve_device(flag: str | None, env: Mapping[str, str]) -> str | None:
    """The microphone to record from: the flag, else the one the plugin is pinned to."""
    return flag or env.get("SPEAK_INPUT_DEVICE") or None


def list_devices() -> list[str]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_device_listing(result.stderr)


def parse_device_listing(listing: str) -> list[str]:
    """Audio input names from ffmpeg's device listing, without the indices, which shift
    whenever a USB device is plugged in or out."""
    names: list[str] = []
    in_audio = False
    for line in listing.splitlines():
        if "audio devices" in line:
            in_audio = True
        elif in_audio:
            match = DEVICE_LINE.search(line)
            if match is None:
                break
            names.append(match.group(1).strip())
    return names


def ffmpeg_command(device: str, dest: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-i", f":{device}",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
        str(dest),
    ]  # fmt: skip


def record_take(device: str, dest: Path) -> bool:
    """Record one take. Writes to a partial file first, so an interrupted take is never
    mistaken for a finished one when the recorder resumes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f"{dest.stem}.partial.wav")
    input("    Enter to start ")
    with subprocess.Popen(ffmpeg_command(device, partial), stdin=subprocess.PIPE) as proc:
        input("    recording - Enter to stop ")
        # "q" stops ffmpeg cleanly so the WAV header records the right length.
        try:
            proc.communicate(b"q", timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
    if not partial.exists() or partial.stat().st_size <= WAV_HEADER_BYTES:
        partial.unlink(missing_ok=True)
        return False
    partial.replace(dest)
    return True


def record(device: str, out: Path, takes: int, sentences: Sequence[str]) -> int:
    todo = list(pending(out, len(sentences), takes))
    total = len(sentences) * takes
    print(f"Microphone  {device}")
    print(f"Dataset     {out}")
    print(f"Progress    {total - len(todo)} of {total} takes\n")
    current = 0
    for sentence, take in todo:
        if take != current:
            current = take
            print(f"-- pass {take} of {takes} --\n")
        dest = take_path(out, sentence, take)
        print(f"[{sentence:03d}]  take {take}   {sentences[sentence - 1]}")
        while True:
            if not record_take(device, dest):
                print("    nothing was captured. Check the name with --list-devices.")
                return 1
            choice = input(f"    {duration(dest):.1f}s   Enter keep   r redo   q quit ").strip()
            if choice.lower() != "r":
                break
            dest.unlink(missing_ok=True)
        if choice.lower() == "q":
            break
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a voice dataset for fine-tuning.")
    parser.add_argument("--device", help="microphone name (default: $SPEAK_INPUT_DEVICE)")
    parser.add_argument("--takes", type=int, default=DEFAULT_TAKES, help="takes per sentence")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="dataset folder")
    parser.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    parser.add_argument("--manifest", action="store_true", help="only rebuild the manifests")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This recorder uses macOS's AVFoundation; it does not run on other platforms yet.")
        return 2
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required: brew install ffmpeg")
        return 2
    if args.takes < 1:
        parser.error("--takes must be at least 1")

    devices = list_devices()
    if args.list_devices:
        print("\n".join(devices))
        return 0

    sentences = read_sentences(SENTENCES)
    out: Path = args.out.expanduser()
    status = 0
    if not args.manifest:
        device = resolve_device(args.device, os.environ)
        if device is None:
            print("Name the microphone you speak into every day, with --device or")
            print("SPEAK_INPUT_DEVICE. Recording on another one costs much of the benefit.\n")
            print("\n".join(f"  {name}" for name in devices))
            return 2
        if device not in devices:
            print(f"No microphone called {device!r}. Available:")
            print("\n".join(f"  {name}" for name in devices))
            return 2
        status = record(device, out, args.takes, sentences)

    if (out / "audio").is_dir():
        train, held_out = build_manifests(out, sentences)
        print(f"\nManifests   {train} training takes, {held_out} held out, in {out}")
    return status


if __name__ == "__main__":
    sys.exit(main())
