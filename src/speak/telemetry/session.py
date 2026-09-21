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
import sqlite3
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path

from speak.queue import UtteranceOutcome
from speak.telemetry import store

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
        store.insert_utterance(
            conn,
            {
                "utterance_id": str(uuid.uuid4()),
                "session_id": session_id,
                "install_id": install_id,
                "seq": store.next_seq(conn, session_id),
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
    _seq: int = field(default=1, repr=False)

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

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
        try:
            store.update_session_harness(self.conn, self.session_id, harness, harness_version)
        except Exception:
            logger.debug("telemetry: failed to update the session's harness", exc_info=True)

    # -- internals, unguarded: callers above catch everything -----------------

    def _write_utterance(self, outcome: UtteranceOutcome) -> None:
        row = {
            "utterance_id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "install_id": self.install_id,
            "seq": self._next_seq(),
            "direction": "out",
            "source": "speak",
            "occurred_at": store.now_iso(),
            "engine": self.tts_engine,
            "model": self.tts_model,
            "voice": outcome.voice,
            "intent": outcome.intent or "unspecified",
            "words": len(outcome.text.split()),
            "chars": len(outcome.text),
            "audio_ms": outcome.audio_ms,
            "queue_wait_ms": outcome.queue_wait_ms,
            "synth_ms": outcome.synth_ms,
            "play_ms": outcome.play_ms,
            "status": outcome.status,
            "text": outcome.text,
            "text_sha256": _sha256_text(outcome.text),
        }
        store.insert_utterance(self.conn, row)

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
            "seq": self._next_seq(),
            "direction": "in",
            "source": source,
            "occurred_at": store.now_iso(),
            "engine": self.asr_engine,
            "model": self.asr_model,
            "device_kind": classify_device(device_name),
            "words": len(transcript.split()) if transcript else None,
            "chars": len(transcript) if transcript else None,
            "audio_ms": audio_ms,
            "asr_ms": asr_ms,
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
        store.insert_utterance(self.conn, row)

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
