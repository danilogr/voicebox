import json

import pytest

from voicebox.transcribe.speakers import (
    UNTAGGED,
    Segment,
    read_segments,
    tag_transcript,
    tag_words,
)

A, B = 1, 2
SEGMENTS = [Segment(0.0, 10.0, A), Segment(10.5, 20.0, B), Segment(18.0, 30.0, A)]


@pytest.mark.parametrize(
    ("start", "end", "speaker", "why"),
    [
        (2.0, 2.5, A, "inside one segment"),
        (9.9, 10.8, B, "straddles a gap: more of it is in B than in A"),
        (9.0, 10.6, A, "straddles a gap: more of it is in A than in B"),
        (10.35, 10.45, B, "in a gap: B starts closer than A ended"),
        (10.1, 10.3, A, "in a gap: A ended closer than B starts"),
        (18.5, 19.0, B, "overlapping speech, equal shares: the earlier segment wins"),
        (19.5, 21.0, A, "overlapping speech: most of it is in the later A"),
        (31.0, 31.5, A, "after the last segment"),
        (5.0, 5.0, A, "a zero-length word inside a segment"),
        (10.125, 10.375, A, "in a gap, as close to both: the earlier segment wins"),
    ],
)
def test_each_word_takes_the_speaker_of_the_segment_it_overlaps_most(start, end, speaker, why):
    """A word's speaker is the segment it shares the most time with, or the nearest one
    when it shares none: a word the diarizer's thresholds left out still belongs to whoever
    was talking around it."""
    [word] = tag_words([{"word": "x", "start": start, "end": end}], SEGMENTS)
    assert word["speaker"] == speaker, why


def test_a_word_before_the_first_segment_takes_the_first_speaker():
    [word] = tag_words([{"word": "x", "start": 0.1, "end": 0.2}], [Segment(1.0, 2.0, B)])
    assert word["speaker"] == B


def test_with_no_segments_every_word_is_untagged():
    """No speech found by the diarizer is not a speaker: NeMo's id for that is 0."""
    words = tag_words([{"word": "x", "start": 1.0, "end": 1.2}], [])
    assert words[0]["speaker"] == UNTAGGED


def test_a_long_segment_that_started_early_still_counts():
    """A segment that began long before a word and is still running overlaps it, even
    with shorter segments starting in between."""
    segments = [Segment(0.0, 100.0, A), Segment(40.0, 41.0, B), Segment(50.0, 51.0, B)]
    [word] = tag_words([{"word": "x", "start": 60.0, "end": 60.5}], segments)
    assert word["speaker"] == A


def test_a_word_after_a_long_segment_takes_it_over_a_shorter_one_inside_it():
    """In a gap, the segment that ended closest before the word is the one reaching
    furthest, not the one that started last."""
    segments = [Segment(0.0, 50.0, A), Segment(10.0, 20.0, B), Segment(70.0, 80.0, B)]
    [word] = tag_words([{"word": "x", "start": 51.0, "end": 52.0}], segments)
    assert word["speaker"] == A


def test_tagging_keeps_every_other_field_and_the_order():
    words = [{"word": "hi", "start": 1.0, "end": 1.2, "confidence": 0.9}]
    [word] = tag_words(words, SEGMENTS)
    assert list(word) == ["word", "start", "end", "confidence", "speaker"]
    assert words[0] == {"word": "hi", "start": 1.0, "end": 1.2, "confidence": 0.9}


def test_segments_are_read_from_the_diarizers_json_in_time_order(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(
        json.dumps(
            {
                "file": "x.wav",
                "segments": [
                    {"start": 5.0, "end": 6.0, "speaker": 2},
                    {"start": 0.0, "end": 1.5, "speaker": 1},
                ],
            }
        )
    )
    assert read_segments(path) == [Segment(0.0, 1.5, 1), Segment(5.0, 6.0, 2)]


NEMO_LAYOUT = """{
  "file": "/x/méeting.wav",
  "text": "Hello there. Hi.",
  "confidence": 1,
  "duration": 45.056,
  "languages": ["en-US"],
  "words": [
    {"word": "Hello", "start": 0.4, "end": 0.72, "confidence": 1, "speaker": 1},
    {"word": "there.", "start": 0.8, "end": 0.88, "confidence": 1, "speaker": 1},
    {"word": "Hi.", "start": 12.0, "end": 12.4, "confidence": 1, "speaker": 2}
  ]
}
"""


def test_a_tagged_transcript_keeps_nemos_layout(tmp_path):
    """Tagging rewrites the transcript in the layout `nemo-speech transcribe --diarize`
    writes, one word to a line, so a reader of either sees the same file."""
    untagged = json.loads(NEMO_LAYOUT)
    for word in untagged["words"]:
        del word["speaker"]
    source = tmp_path / "j1.untagged"
    source.write_text(json.dumps(untagged))
    output = tmp_path / "t.json"

    tag_transcript(source, [Segment(0.0, 5.0, 1), Segment(11.0, 13.0, 2)], output)

    assert output.read_text(encoding="utf-8") == NEMO_LAYOUT
    assert sorted(tmp_path.iterdir()) == [source, output]


def test_a_transcript_that_cannot_be_written_leaves_nothing_half_written(tmp_path):
    source = tmp_path / "j1.untagged"
    source.write_text(NEMO_LAYOUT)
    output = tmp_path / "t.json"
    output.mkdir()  # nothing can replace a directory that holds a file
    (output / "keep").write_text("")

    with pytest.raises(OSError):
        tag_transcript(source, [Segment(0.0, 5.0, 1)], output)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["j1.untagged", "t.json"]
