"""Turns queue outcomes and listen/talk attempts into stored rows, for one session.

Three rules, in priority order: never block or delay speech, never break the plugin,
never send content. The first two are why every public method here is synchronous and
every failure is caught and logged at debug rather than raised - a dead telemetry
backend has to be indistinguishable from a healthy one. The third is enforced in
`ship.py`, by construction: this module freely writes content columns to the local
store, and only the upload projection drops them.
"""

import hashlib
import io
import logging
import re
import sqlite3
import uuid
import wave
from dataclasses import dataclass, replace
from pathlib import Path

from speak_core.engines import nemo, say
from speak_core.queue import UtteranceOutcome
from speak_core.telemetry import store

from speak.telemetry.contract import SCHEMA

logger = logging.getLogger(__name__)

_BLUETOOTH_HINTS = ("airpods", "beats", "bluetooth", "headset")
_USB_HINTS = ("usb",)
_VIRTUAL_HINTS = ("loopback", "virtual", "blackhole", "soundflower", "aggregate")
_BUILTIN_HINTS = ("built-in", "macbook", "internal")


def classify_device(name: str | None) -> str:
    """A coarse guess at the input's kind from its free-text name.

    Approximate by construction: the name itself is never uploaded (spec 1, "two
    columns that must not be uploaded"), so this five-value guess is what a
    session's hardware mix looks like from telemetry instead of the name.
    """
    if not name:
        return "unknown"
    lowered = name.lower()
    if any(hint in lowered for hint in _BLUETOOTH_HINTS):
        return "bluetooth"
    if any(hint in lowered for hint in _USB_HINTS):
        return "usb"
    if any(hint in lowered for hint in _VIRTUAL_HINTS):
        return "virtual"
    if any(hint in lowered for hint in _BUILTIN_HINTS):
        return "builtin"
    return "unknown"


# Lower-cased name -> the engine's own spelling, for every voice that may be
# recorded as itself. Keyed by engine because a name is only known for the engine
# that ships it; `command` has none, so every voice it is given is `custom`.
_KNOWN_VOICES: dict[str, dict[str, str]] = {
    engine: {name.lower(): name for name in names}
    for engine, names in (("say", say.KNOWN_VOICES), ("nemo", nemo.KNOWN_VOICES))
}
CUSTOM_VOICE = "custom"


def recorded_voice(engine: str, voice: str | None) -> str | None:
    """The voice as an `out` row may carry it: a known name in the engine's own
    spelling, `custom` for any other name, `None` when the engine's default ran.

    A voice name is free text - `SPEAK_VOICE`, a `speak(voice=...)` argument, a
    third-party voice installed for `say` - and free text never ships.
    """
    if not voice:
        return None
    return _KNOWN_VOICES.get(engine, {}).get(voice.lower(), CUSTOM_VOICE)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wav_duration_ms(wav_bytes: bytes) -> int | None:
    try:
        with wave.open(io.BytesIO(wav_bytes)) as handle:
            frames: int = handle.getnframes()
            rate: int = handle.getframerate()
    except (wave.Error, EOFError) as error:
        logger.debug("could not read WAV duration: %s", error)
        return None
    if rate <= 0:
        return None
    return round(frames / rate * 1000)


@dataclass(frozen=True, slots=True)
class TranscriptionOutcome:
    """The shape of one transcription, however far it got. Never its content."""

    status: str  # ok | failed | refused | heard_nothing
    source_bytes: int | None
    audio_codec: str | None
    audio_ms: int | None
    asr_ms: int | None
    converted: bool | None
    diarize: bool
    format: str
    words: int | None = None
    chars: int | None = None


# What an `in`/`transcribe` row may carry, from the vendored contract
# (`tests/contract/v1/schema/ingest-request.schema.json`). Respecting them is the
# client's job: the server answers a row above a ceiling with a 400, and the
# shipper sets that whole chunk aside for good - one eleven-gigabyte video would
# cost every row that happened to ship beside it.
MAX_SOURCE_BYTES = 10_737_418_240
MAX_AUDIO_MS = 86_400_000
MAX_ASR_MS = 86_400_000
MAX_WORDS = 500_000
MAX_CHARS = 5_000_000

# The same four columns' ceilings for every other row - `speak`, `listen`, `talk` -
# from the same contract's `else` branch, keyed on `source != "transcribe"`. Tighter,
# because only a transcription gets the long-file exemption: a `listen` recording
# past ten minutes (`SPEAK_LISTEN_MAX_SECONDS` set above 600) or an outsized `speak`
# utterance would otherwise land the same 400, and lose the same chunk.
ROW_MAX_AUDIO_MS = 600_000
ROW_MAX_ASR_MS = 120_000
ROW_MAX_WORDS = 20_000
ROW_MAX_CHARS = 150_000

# The `out` row's timings, from the same contract. A sentence stuck behind a wedged
# queue, or a player that hangs, would otherwise measure past them.
MAX_QUEUE_WAIT_MS = 300_000
MAX_SYNTH_MS = 120_000
MAX_PLAY_MS = 600_000

# `harness_version` is whatever the MCP client declares about itself; the contract
# caps its length, and the session row travels with every chunk.
HARNESS_VERSION_MAX_LENGTH = 64

# From the same contract: `audio_codec` must match this shape and length
# (`tests/contract/v1/schema/ingest-request.schema.json`). Every codec ffprobe
# reports matches both; a malformed probe or an exotic container might not, and the
# server answers a non-matching value with the same 400 that ends a chunk's
# unrelated speak/listen rows along with it - so a codec that would be rejected is
# dropped here rather than sent.
AUDIO_CODEC_PATTERN = re.compile(r"^[a-z0-9_]+$")
AUDIO_CODEC_MAX_LENGTH = 32


def clamped(outcome: TranscriptionOutcome) -> TranscriptionOutcome:
    """The same outcome with every measured value inside the contract's range.

    A clamped number is a statement about a ceiling rather than a measurement, which
    is the right trade: the alternative to one approximate row is no rows at all.
    """
    return replace(
        outcome,
        source_bytes=_within(outcome.source_bytes, MAX_SOURCE_BYTES),
        audio_codec=_codec_or_none(outcome.audio_codec),
        audio_ms=_within(outcome.audio_ms, MAX_AUDIO_MS),
        asr_ms=_within(outcome.asr_ms, MAX_ASR_MS),
        words=_within(outcome.words, MAX_WORDS),
        chars=_within(outcome.chars, MAX_CHARS),
    )


def _codec_or_none(codec: str | None) -> str | None:
    """None rather than a value the contract's pattern or length would refuse.

    `codec` is typed `str | None`, but it starts life as whatever a job record on
    disk says `codec` is - JSON that a hand edit or a corrupted write could give any
    shape at all - so the type is checked before the pattern is matched against it,
    rather than trusting the annotation and raising a `TypeError` out of the
    telemetry write.
    """
    if not isinstance(codec, str):
        return None
    if len(codec) > AUDIO_CODEC_MAX_LENGTH or not AUDIO_CODEC_PATTERN.fullmatch(codec):
        return None
    return codec


def _within(value: int | None, ceiling: int) -> int | None:
    """Inside [0, ceiling]: the contract's minimum is as binding as its maximum."""
    if value is None:
        return None
    return min(max(value, 0), ceiling)


def record_transcription(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    install_id: str,
    engine: str,
    model: str | None,
    outcome: TranscriptionOutcome,
) -> None:
    """Record one transcription as an `in` utterance from the `transcribe` source.

    A function rather than a `TelemetrySession` method: the worker that finishes a
    job is its own process, possibly long after the session that asked for it has
    ended, and it has the store and the ids but not the session object. Not
    `heard()` either, which stamps a `device_kind` the contract forbids on a
    transcription. Never raises.
    """
    try:
        store.insert_row(
            conn,
            SCHEMA.rows,
            {
                "utterance_id": str(uuid.uuid4()),
                "session_id": session_id,
                "install_id": install_id,
                "direction": "in",
                "source": "transcribe",
                "occurred_at": store.now_iso(),
                "engine": engine,
                "model": model,
                "words": outcome.words,
                "chars": outcome.chars,
                "audio_ms": outcome.audio_ms,
                "asr_ms": outcome.asr_ms,
                "status": outcome.status,
                "source_bytes": outcome.source_bytes,
                "audio_codec": outcome.audio_codec,
                "converted": outcome.converted,
                "diarize": outcome.diarize,
                "format": outcome.format,
            },
        )
    except Exception:
        logger.debug("telemetry: failed to record a transcription", exc_info=True)


@dataclass(slots=True)
class TelemetrySession:
    """One session's write path into the local store.

    `tts_engine`/`tts_model` describe `out` rows; `asr_engine`/`asr_model` describe
    `in` rows - the contract's generic `engine`/`model` columns mean different
    things depending on direction, and this is where that distinction is made.
    """

    conn: sqlite3.Connection
    install_id: str
    session_id: str
    tts_engine: str
    tts_model: str | None
    asr_engine: str | None
    asr_model: str | None
    record_audio: bool = False
    recordings_dir: Path | None = None
    db_path: Path | None = None

    def utterance_finished(self, outcome: UtteranceOutcome) -> None:
        """Record one utterance `speak` produced - the queue's `on_finish` hook."""
        try:
            self._write_utterance(outcome)
        except Exception:
            logger.debug("telemetry: failed to record a spoken utterance", exc_info=True)

    def heard(
        self,
        *,
        source: str,
        status: str,
        transcript: str | None = None,
        device_name: str | None = None,
        asr_ms: int | None = None,
        wav: bytes | None = None,
    ) -> None:
        """Record one `listen` or `/talk` attempt, however far it got."""
        try:
            self._write_heard(
                source=source,
                status=status,
                transcript=transcript,
                device_name=device_name,
                asr_ms=asr_ms,
                wav=wav,
            )
        except Exception:
            logger.debug("telemetry: failed to record a heard utterance", exc_info=True)

    def close(self) -> None:
        try:
            store.end_session(self.conn, self.session_id, store.now_iso())
        except Exception:
            logger.debug("telemetry: failed to close the session row", exc_info=True)

    def update_harness(self, harness: str, harness_version: str | None) -> None:
        """Correct the harness the session row was opened with, once spec 2's
        better source (MCP `client_info`) is known."""
        if harness_version is not None:
            harness_version = harness_version[:HARNESS_VERSION_MAX_LENGTH]
        try:
            store.update_session_harness(self.conn, self.session_id, harness, harness_version)
        except Exception:
            logger.debug("telemetry: failed to update the session's harness", exc_info=True)

    # -- internals, unguarded: callers above catch everything -----------------

    def _write_utterance(self, outcome: UtteranceOutcome) -> None:
        # A fallback reports the engine that actually spoke; the configured model
        # belongs to the configured engine and says nothing about any other.
        engine = outcome.engine or self.tts_engine
        model = self.tts_model if engine == self.tts_engine else None
        row = {
            "utterance_id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "install_id": self.install_id,
            "direction": "out",
            "source": "speak",
            "occurred_at": store.now_iso(),
            "engine": engine,
            "model": model,
            "voice": recorded_voice(engine, outcome.voice),
            "intent": outcome.intent or "unspecified",
            "words": _within(len(outcome.text.split()), ROW_MAX_WORDS),
            "chars": _within(len(outcome.text), ROW_MAX_CHARS),
            "audio_ms": _within(outcome.audio_ms, ROW_MAX_AUDIO_MS),
            "queue_wait_ms": _within(outcome.queue_wait_ms, MAX_QUEUE_WAIT_MS),
            "synth_ms": _within(outcome.synth_ms, MAX_SYNTH_MS),
            "play_ms": _within(outcome.play_ms, MAX_PLAY_MS),
            "status": outcome.status,
            "text": outcome.text,
            "text_sha256": _sha256_text(outcome.text),
        }
        store.insert_row(self.conn, SCHEMA.rows, row)

    def _write_heard(
        self,
        *,
        source: str,
        status: str,
        transcript: str | None,
        device_name: str | None,
        asr_ms: int | None,
        wav: bytes | None,
    ) -> None:
        audio_ms = wav_duration_ms(wav) if wav else None
        row: dict[str, object] = {
            "utterance_id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "install_id": self.install_id,
            "direction": "in",
            "source": source,
            "occurred_at": store.now_iso(),
            "engine": self.asr_engine,
            "model": self.asr_model,
            "device_kind": classify_device(device_name),
            "words": _within(len(transcript.split()) if transcript else None, ROW_MAX_WORDS),
            "chars": _within(len(transcript) if transcript else None, ROW_MAX_CHARS),
            "audio_ms": _within(audio_ms, ROW_MAX_AUDIO_MS),
            "asr_ms": _within(asr_ms, ROW_MAX_ASR_MS),
            "status": status,
            "text": transcript,
            "text_sha256": _sha256_text(transcript) if transcript else None,
            "device_name": device_name,
        }
        if self.record_audio and wav and self.recordings_dir is not None:
            utterance_id = str(row["utterance_id"])
            path, digest = self._store_recording(self.recordings_dir, utterance_id, wav)
            if path is not None:
                row["audio_path"] = str(path)
                row["audio_sha256"] = digest
        store.insert_row(self.conn, SCHEMA.rows, row)

    def _store_recording(
        self, directory: Path, utterance_id: str, wav_bytes: bytes
    ) -> tuple[Path | None, str | None]:
        """Write one recording to disk when the user opted in. Never uploaded under
        any setting (spec 6); a write failure here must not cost the telemetry row."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{utterance_id}.wav"
            path.write_bytes(wav_bytes)
        except OSError as error:
            logger.debug("could not save recording: %s", error)
            return None, None
        return path, _sha256_bytes(wav_bytes)
