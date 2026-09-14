"""The voice-tuning recorder's dataset logic. No microphone and no ffmpeg: every test
builds WAV files directly, so it runs on any machine."""

import importlib.util
import json
import wave
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "voice_tuning_record", Path(__file__).parents[1] / "voice-tuning" / "record.py"
)
assert _SPEC is not None and _SPEC.loader is not None
record = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(record)

SENTENCES = [f"sentence number {n}" for n in range(1, 26)]


def _wav(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(record.SAMPLE_RATE)
        audio.writeframes(b"\x00\x00" * int(record.SAMPLE_RATE * seconds))


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _record_all(out: Path, sentences: int, takes: int) -> None:
    for sentence in range(1, sentences + 1):
        for take in range(1, takes + 1):
            _wav(record.take_path(out, sentence, take))


def test_no_sentence_is_both_trained_on_and_used_to_measure(tmp_path):
    """With several takes per sentence, splitting by file would put take 1 of a sentence
    in training and take 2 in the held-out set - scoring the model on a sentence it has
    already heard in this voice."""
    _record_all(tmp_path, sentences=25, takes=3)
    record.build_manifests(tmp_path, SENTENCES)

    trained = {row["text"] for row in _rows(tmp_path / record.TRAIN_MANIFEST)}
    measured = {row["text"] for row in _rows(tmp_path / record.VAL_MANIFEST)}

    assert trained.isdisjoint(measured)
    assert measured == {SENTENCES[9], SENTENCES[19]}
    assert len(_rows(tmp_path / record.VAL_MANIFEST)) == 6  # both held-out sentences, every take


def test_manifest_paths_resolve_against_the_manifest_folder(tmp_path):
    """NeMo resolves a relative audio_filepath against the manifest's directory, so the
    dataset folder can be copied to the training machine unchanged."""
    _record_all(tmp_path, sentences=3, takes=2)
    record.build_manifests(tmp_path, SENTENCES)

    for row in _rows(tmp_path / record.TRAIN_MANIFEST):
        path = str(row["audio_filepath"])
        assert not Path(path).is_absolute()
        assert (tmp_path / path).is_file()


def test_every_row_carries_exactly_what_nemo_reads(tmp_path):
    _wav(record.take_path(tmp_path, 1, 1), seconds=2.5)
    record.build_manifests(tmp_path, SENTENCES)

    (row,) = _rows(tmp_path / record.TRAIN_MANIFEST)
    assert row == {"audio_filepath": "audio/001-1.wav", "duration": 2.5, "text": SENTENCES[0]}


def test_a_missing_take_is_left_out_rather_than_mislabelled(tmp_path):
    _record_all(tmp_path, sentences=5, takes=2)
    record.take_path(tmp_path, 3, 2).unlink()
    record.build_manifests(tmp_path, SENTENCES)

    files = [row["audio_filepath"] for row in _rows(tmp_path / record.TRAIN_MANIFEST)]
    assert "audio/003-2.wav" not in files
    assert files.count("audio/003-1.wav") == 1
    assert len(files) == 9


def test_interrupted_and_unreadable_takes_are_skipped(tmp_path):
    _wav(record.take_path(tmp_path, 1, 1))
    _wav(tmp_path / "audio" / "002-1.partial.wav")
    (tmp_path / "audio" / "003-1.wav").write_bytes(b"not a wav file")
    record.build_manifests(tmp_path, SENTENCES)

    files = [row["audio_filepath"] for row in _rows(tmp_path / record.TRAIN_MANIFEST)]
    assert files == ["audio/001-1.wav"]


def test_takes_are_recorded_in_passes_not_back_to_back(tmp_path):
    """A sentence read three times in a row copies its own delivery; every sentence is
    read once before any is read again."""
    order = list(record.pending(tmp_path, count=3, takes=2))
    assert order == [(1, 1), (2, 1), (3, 1), (1, 2), (2, 2), (3, 2)]


def test_resuming_picks_up_at_the_first_unrecorded_take(tmp_path):
    _wav(record.take_path(tmp_path, 1, 1))
    _wav(record.take_path(tmp_path, 2, 1))
    assert next(record.pending(tmp_path, count=3, takes=2)) == (3, 1)


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        ("Studio Mic", {"SPEAK_INPUT_DEVICE": "MacBook Pro Microphone"}, "Studio Mic"),
        (None, {"SPEAK_INPUT_DEVICE": "MacBook Pro Microphone"}, "MacBook Pro Microphone"),
        (None, {}, None),
        (None, {"SPEAK_INPUT_DEVICE": ""}, None),
    ],
)
def test_the_microphone_defaults_to_the_one_the_plugin_is_pinned_to(flag, env, expected):
    assert record.resolve_device(flag, env) == expected


def test_a_device_listing_yields_audio_names_without_their_shifting_indices():
    listing = (
        "[AVFoundation indev @ 0x1] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x1] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x1] [0] Anker PowerConf C200\n"
        "[AVFoundation indev @ 0x1] [1] MacBook Pro Microphone\n"
        "[in#0 @ 0x2] Error opening input: Input/output error\n"
    )
    assert record.parse_device_listing(listing) == [
        "Anker PowerConf C200",
        "MacBook Pro Microphone",
    ]
