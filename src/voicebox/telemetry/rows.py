"""One `jobs` row per finished job, in the shape contract v1 accepts.

Every value is checked here, before the row is stored, because the server answers a
row outside the contract with a `400` and the shipper then sets aside the whole chunk
it travelled in: one oversized file would cost every job shipped beside it. A number
past its ceiling is clamped, a string that does not fit its pattern is dropped (a voice
name is recorded as `custom` before it gets here), and a reason or stage outside the
closed sets becomes `other` or the kind's own stage. A row asking for a field its kind
does not carry, or a status its kind cannot have, is a bug in the caller and raises.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeGuard

from speak_core.engines import nemo, say

TRANSCRIPTION = "transcription"
SYNTHESIS = "synthesis"
NARRATION = "narration"

STATUSES: dict[str, frozenset[str]] = {
    TRANSCRIPTION: frozenset({"ok", "empty", "refused", "failed", "crashed"}),
    SYNTHESIS: frozenset({"ok", "refused", "failed", "crashed"}),
    NARRATION: frozenset({"ok", "stopped", "refused", "failed"}),
}
REASONS = frozenset(
    {
        "busy",
        "input_missing",
        "input_unsupported",
        "output_unwritable",
        "ffmpeg_missing",
        "nemo_missing",
        "server_unavailable",
        "timeout",
        "conversion_failed",
        "model_failed",
        "format_mismatch",
        "other",
    }
)
STAGES = frozenset({"probe", "convert", "asr", "split", "synthesize", "encode", "write", "play"})
OTHER = "other"
# Where a kind most likely stopped when nothing better is known.
DEFAULT_STAGE = {TRANSCRIPTION: "asr", SYNTHESIS: "synthesize", NARRATION: "play"}

MAX_WALL_MS = 172_800_000  # 48 h, every kind
_GIB_10 = 10_737_418_240
_DAY_MS = 86_400_000
_TWO_DAYS_MS = 172_800_000

# Each kind's integer fields and their ceilings (the contract's maxima; every minimum is 0).
INT_CEILINGS: dict[str, dict[str, int]] = {
    TRANSCRIPTION: {
        "source_bytes": _GIB_10,
        "audio_ms": _DAY_MS,
        "convert_ms": _DAY_MS,
        "asr_ms": _DAY_MS,
        "speakers": 64,
        "words": 500_000,
        "chars": 5_000_000,
    },
    SYNTHESIS: {
        "chars": 5_000_000,
        "words": 1_000_000,
        "paragraphs": 100_000,
        "chunks": 100_000,
        "chunks_done": 100_000,
        "synth_ms": _TWO_DAYS_MS,
        "encode_ms": _DAY_MS,
        "audio_ms": _TWO_DAYS_MS,
        "output_bytes": _GIB_10,
    },
    NARRATION: {
        "sentences": 100_000,
        "sentences_played": 100_000,
        "chars": 5_000_000,
        "synth_ms": _TWO_DAYS_MS,
        "audio_ms": _TWO_DAYS_MS,
        "fallback_sentences": 100_000,
    },
}
BOOLEANS: dict[str, frozenset[str]] = {
    TRANSCRIPTION: frozenset({"has_video", "converted", "diarize", "translate"}),
    SYNTHESIS: frozenset({"server_warm"}),
    NARRATION: frozenset({"server_warm"}),
}
ENUMS: dict[str, dict[str, frozenset[str]]] = {
    TRANSCRIPTION: {"format": frozenset({"text", "json", "srt", "vtt"})},
    SYNTHESIS: {
        "input_kind": frozenset({"text", "file"}),
        "input_format": frozenset({"plain", "markdown"}),
        "format": frozenset({"wav", "m4a", "mp3"}),
    },
    NARRATION: {},
}
# The contract's uuid: version 4 only.
UUID4 = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
# The contract's timestamp, as this client writes it: UTC with a `Z` (`iso`, core's `now_iso`).
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
# Patterns and maximum lengths of every free-form string the contract accepts.
STRINGS: dict[str, tuple[re.Pattern[str], int]] = {
    "engine": (_IDENTIFIER, 32),
    "model": (_IDENTIFIER, 64),
    "voice": (_IDENTIFIER, 64),
    "audio_codec": (re.compile(r"^[a-z0-9_]+$"), 32),
    "language": (re.compile(r"^(auto|[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*)$"), 35),
}
KIND_STRINGS: dict[str, frozenset[str]] = {
    TRANSCRIPTION: frozenset({"audio_codec", "language"}),
    SYNTHESIS: frozenset({"voice"}),
    NARRATION: frozenset({"voice"}),
}

CUSTOM_VOICE = "custom"
UNKNOWN_ENGINE = "unknown"

# Lower-cased name -> the engine's own spelling, for every voice recorded as itself.
_KNOWN_VOICES: dict[str, dict[str, str]] = {
    engine: {name.lower(): name for name in names}
    for engine, names in (("say", say.KNOWN_VOICES), ("nemo", nemo.KNOWN_VOICES))
}


def recorded_voice(engine: str, voice: str | None) -> str | None:
    """A voice as a row may carry it: a name the engine ships, in its own spelling;
    `custom` for any other name (free text never ships); `None` when none was named."""
    if not voice:
        return None
    return _KNOWN_VOICES.get(engine, {}).get(voice.lower(), CUSTOM_VOICE)


def iso(epoch: float) -> str:
    """A wall-clock instant as the contract's timestamp: UTC, with a `Z`."""
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def wall_ms(
    created_at: float, created_monotonic: float | None, *, now: float, now_monotonic: float
) -> int:
    """How long a job took, measured on the monotonic clock when that clock still spans
    it (it is system-wide, so another process can read it) and on the wall clock when it
    was not recorded or a reboot has reset it."""
    if created_monotonic is None or now_monotonic < created_monotonic:
        elapsed = now - created_at
    else:
        elapsed = now_monotonic - created_monotonic
    return max(0, round(elapsed * 1000))


@dataclass(frozen=True, slots=True)
class JobFacts:
    """The columns every kind has."""

    job_id: str
    session_id: str
    install_id: str
    kind: str
    started_at: str
    ended_at: str
    wall_ms: int
    status: str
    engine: str
    reason: str | None = None
    stage: str | None = None
    model: str | None = None
    # Local only: never uploaded (the contract's `-- LOCAL` columns).
    error_message: str | None = None
    source_name: str | None = None
    output_path: str | None = None


def job_row(facts: JobFacts, fields: Mapping[str, object] | None = None) -> dict[str, object]:
    """One `jobs` row: the common facts plus the kind's own fields, every value inside
    the contract. `None` values stay `None` (they are stored as NULL and never shipped).

    Raises:
        ValueError: for an unknown kind, a status the kind cannot have, or a field the
            kind does not carry - a caller's bug, not data to clamp.
    """
    kind = facts.kind
    if kind not in STATUSES:
        raise ValueError(f"unknown job kind {kind!r}")
    for stamp in (facts.started_at, facts.ended_at):
        if not TIMESTAMP.fullmatch(stamp):
            raise ValueError(f"{stamp!r} is not a UTC timestamp the contract accepts")
    for name, ident in (
        ("job_id", facts.job_id),
        ("session_id", facts.session_id),
        ("install_id", facts.install_id),
    ):
        if not UUID4.fullmatch(ident):
            raise ValueError(f"{name} {ident!r} is not a version 4 uuid")
    if facts.status not in STATUSES[kind]:
        raise ValueError(f"a {kind} cannot be {facts.status!r}")
    if isinstance(facts.wall_ms, bool) or not _finite(facts.wall_ms):
        raise ValueError(f"wall_ms {facts.wall_ms!r} is not a finite number")
    engine = _string("engine", facts.engine) or UNKNOWN_ENGINE
    row: dict[str, object] = {
        "job_id": facts.job_id,
        "session_id": facts.session_id,
        "install_id": facts.install_id,
        "kind": kind,
        "started_at": facts.started_at,
        "ended_at": facts.ended_at,
        "wall_ms": _within(round(facts.wall_ms), MAX_WALL_MS),
        "status": facts.status,
        "reason": _reason(facts),
        "stage": _stage(facts),
        "engine": engine,
        "model": _string("model", facts.model),
        "error_message": facts.error_message,
        "source_name": facts.source_name,
        "output_path": facts.output_path,
    }
    for name, value in (fields or {}).items():
        row[name] = _field(kind, engine, name, value)
    return row


def _field(kind: str, engine: str, name: str, value: object) -> object:
    if name in BOOLEANS[kind]:
        return None if value is None else bool(value)
    if name in INT_CEILINGS[kind]:
        if isinstance(value, bool) or not _finite(value):
            return None
        return _within(round(value), INT_CEILINGS[kind][name])
    if name in ENUMS[kind]:
        return value if value in ENUMS[kind][name] else None
    if name == "voice" and name in KIND_STRINGS[kind]:
        # Free text never ships: only a name the engine ships is recorded as itself.
        return _string(name, recorded_voice(engine, _string(name, value)))
    if name in KIND_STRINGS[kind]:
        return _string(name, value)
    raise ValueError(f"a {kind} row does not carry {name!r}")


def _reason(facts: JobFacts) -> str | None:
    if facts.status not in ("failed", "refused"):
        return None
    return facts.reason if facts.reason in REASONS else OTHER


def _stage(facts: JobFacts) -> str | None:
    if facts.status not in ("failed", "crashed"):
        return None
    return facts.stage if facts.stage in STAGES else DEFAULT_STAGE[facts.kind]


def _string(name: str, value: object) -> str | None:
    """`value` stripped, if it fits the contract's pattern and length; else `None`.

    `fullmatch` on the stripped text, never the pattern alone: Python's `$` also matches
    before a trailing newline, which the server's JavaScript patterns reject.
    """
    if not isinstance(value, str):
        return None
    pattern, longest = STRINGS[name]
    value = value.strip()
    if len(value) > longest or not pattern.fullmatch(value):
        return None
    return value


def _finite(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and math.isfinite(value)


def _within(value: int, ceiling: int) -> int:
    return min(max(value, 0), ceiling)
