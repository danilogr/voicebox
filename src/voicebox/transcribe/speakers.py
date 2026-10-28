"""Speaker tags for a json transcript's words, from `nemo-speech diarize`.

`nemo-speech transcribe --diarize` (NeMo-Speech.cpp 0.2.0) tags only the words inside
the diarizer's retained window - about the last fifteen minutes, once a recording passes
twenty - and gives every older word one speaker. The standalone diarizer reports every
segment of the file, so the worker transcribes without `--diarize` and tags the words
from those segments here.
"""

import json
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNTAGGED = 0  # NeMo's speaker id for a word with no speaker


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    speaker: int  # 1-based, as in the transcript


def read_segments(path: Path) -> list[Segment]:
    """The segments of `nemo-speech diarize --format json`, in time order."""
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = (
        Segment(float(s["start"]), float(s["end"]), int(s["speaker"])) for s in data["segments"]
    )
    return sorted(segments, key=lambda s: s.start)


def tag_words(words: Sequence[dict[str, Any]], segments: Sequence[Segment]) -> list[dict[str, Any]]:
    """Copies of `words`, each with the speaker of the segment it overlaps most, or of the
    nearest segment when it overlaps none: the diarizer's thresholds leave short gaps and
    edges out of every segment, and a word there still belongs to whoever was talking."""
    timeline = _Timeline(segments)
    return [{**word, "speaker": timeline.speaker(word["start"], word["end"])} for word in words]


def tag_transcript(untagged: Path, segments: Sequence[Segment], output: Path) -> None:
    """Write the json transcript `untagged` to `output` with its words tagged, keeping
    NeMo-Speech.cpp's layout. `output` appears only once it is whole."""
    data = json.loads(untagged.read_text(encoding="utf-8"))
    data["words"] = tag_words(data.get("words") or [], segments)
    partial = output.with_name(f".{output.name}.partial")
    try:
        partial.write_text(_nemo_layout(data), encoding="utf-8")
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


class _Timeline:
    """Segments sorted by start, with the furthest-reaching one so far at each index, so
    finding the segments a word overlaps never scans the whole file."""

    def __init__(self, segments: Sequence[Segment]) -> None:
        self._segments = sorted(segments, key=lambda s: s.start)
        self._starts = [s.start for s in self._segments]
        self._furthest: list[int] = []
        for i, segment in enumerate(self._segments):
            reach = self._furthest[-1] if self._furthest else i
            further = segment.end > self._segments[reach].end
            self._furthest.append(i if further else reach)

    def speaker(self, start: float, end: float) -> int:
        if not self._segments:
            return UNTAGGED
        # The last segment starting before the word ends; every overlapping segment is at
        # or before it, and only as far back as some segment still reaches the word.
        last = bisect_left(self._starts, end) - 1
        candidates = set()
        if last + 1 < len(self._segments):
            candidates.add(last + 1)  # the next one to start: nearest if nothing overlaps
        if last >= 0:
            candidates.add(self._furthest[last])  # ended nearest before, if none overlaps
        i = last
        while i >= 0 and self._segments[self._furthest[i]].end > start:
            candidates.add(i)
            i -= 1
        # Shared time, negative for the gap to a segment that does not overlap. Ties go
        # to the segment that started first.
        best = max(
            sorted(candidates),
            key=lambda i: min(end, self._segments[i].end) - max(start, self._segments[i].start),
        )
        return self._segments[best].speaker


def _nemo_layout(data: dict[str, Any]) -> str:
    """One top-level field to a line and one word to a line, as NeMo-Speech.cpp writes."""
    fields = []
    for key, value in data.items():
        if key == "words" and value:
            words = ",\n".join(f"    {json.dumps(w, ensure_ascii=False)}" for w in value)
            fields.append(f'  "words": [\n{words}\n  ]')
        else:
            fields.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
    return "{\n" + ",\n".join(fields) + "\n}\n"
