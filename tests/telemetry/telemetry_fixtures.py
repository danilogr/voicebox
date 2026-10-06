"""Two telemetry schemas for the store and shipper tests - one shaped like the
utterance contract, one like a job contract - which the code under test must handle
without knowing either."""

from pathlib import Path

from speak.telemetry.schema import RowTable, TelemetrySchema

UTTERANCES = TelemetrySchema(
    sql=Path(__file__).with_name("utterances_schema.sql").read_text(),
    session_columns=(
        "session_id",
        "install_id",
        "started_at",
        "ended_at",
        "plugin_version",
        "tts_engine",
        "asr_engine",
        "listen_enabled",
        "harness",
        "harness_version",
    ),
    session_boolean_columns=("listen_enabled",),
    rows=RowTable(
        name="utterances",
        id_column="utterance_id",
        order_column="occurred_at",
        columns=(
            "utterance_id",
            "session_id",
            "install_id",
            "seq",
            "direction",
            "source",
            "occurred_at",
            "engine",
            "model",
            "voice",
            "device_kind",
            "intent",
            "words",
            "chars",
            "audio_ms",
            "queue_wait_ms",
            "synth_ms",
            "play_ms",
            "asr_ms",
            "status",
            "source_bytes",
            "audio_codec",
            "converted",
            "diarize",
            "format",
            "text",
            "text_sha256",
            "audio_path",
            "audio_sha256",
            "device_name",
            "corrected_text",
            "corrected_at",
            "correction_kind",
            "shipped_at",
            "rejected_at",
        ),
        local_columns=frozenset(
            {
                "text",
                "text_sha256",
                "audio_path",
                "audio_sha256",
                "device_name",
                "corrected_text",
                "corrected_at",
                "correction_kind",
                "shipped_at",
                "rejected_at",
            }
        ),
        boolean_columns=("converted", "diarize"),
        seq_column="seq",
    ),
)

JOBS_SQL = """
CREATE TABLE installs (
  install_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  plugin_version TEXT NOT NULL, disclosure_ver INTEGER, country TEXT, language TEXT,
  timezone TEXT, utc_offset_min INTEGER, os TEXT NOT NULL, os_version TEXT,
  arch TEXT NOT NULL, cpu_model TEXT, cpu_count INTEGER, ram_gb INTEGER, accel TEXT,
  accel_name TEXT
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, install_id TEXT NOT NULL REFERENCES installs(install_id),
  started_at TEXT NOT NULL, ended_at TEXT, plugin_version TEXT NOT NULL, core_version TEXT,
  harness TEXT NOT NULL, harness_version TEXT, ffmpeg_present INTEGER, shipped_at TEXT
);
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
  install_id TEXT NOT NULL, kind TEXT NOT NULL, started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL, status TEXT NOT NULL, has_video INTEGER, error_message TEXT,
  shipped_at TEXT, rejected_at TEXT
);
"""

JOBS = TelemetrySchema(
    sql=JOBS_SQL,
    session_columns=(
        "session_id",
        "install_id",
        "started_at",
        "ended_at",
        "plugin_version",
        "core_version",
        "harness",
        "harness_version",
        "ffmpeg_present",
    ),
    session_boolean_columns=("ffmpeg_present",),
    rows=RowTable(
        name="jobs",
        id_column="job_id",
        order_column="ended_at",
        columns=(
            "job_id",
            "session_id",
            "install_id",
            "kind",
            "started_at",
            "ended_at",
            "status",
            "has_video",
            "error_message",
            "shipped_at",
            "rejected_at",
        ),
        local_columns=frozenset({"error_message", "shipped_at", "rejected_at"}),
        boolean_columns=("has_video",),
    ),
)
