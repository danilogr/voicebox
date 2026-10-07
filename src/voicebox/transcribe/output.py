"""Where a transcript goes, and the words inside it whatever its format."""

import json
import re
from pathlib import Path

EXTENSIONS = {"text": "txt", "json": "json", "srt": "srt", "vtt": "vtt"}
FORMATS = tuple(EXTENSIONS)

# Cue numbers, timing lines and the WebVTT header: everything in a subtitle file
# that is not something somebody said.
_NOT_SPEECH = re.compile(r"^\d+$|-->|^WEBVTT")


def output_path(source: Path, directory: Path, fmt: str, job_id: str) -> Path:
    """`<stem>.<ext>`, or `<stem>.<job_id>.<ext>` when that is taken - a new
    transcript never overwrites an earlier one."""
    ext = EXTENSIONS[fmt]
    plain = directory / f"{source.stem}.{ext}"
    return plain if not plain.exists() else directory / f"{source.stem}.{job_id}.{ext}"


def transcript_text(path: Path, fmt: str) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if fmt == "json":
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
        text = data.get("text") if isinstance(data, dict) else None
        return text.strip() if isinstance(text, str) else ""
    if fmt in ("srt", "vtt"):
        lines = (line.strip() for line in raw.splitlines())
        return " ".join(line for line in lines if line and not _NOT_SPEECH.search(line))
    return raw.strip()
