"""This plugin's telemetry contract as speak-core's store and shipper need it.

The schema is `speak-telemetry`'s `contract/v1/schema.sql`, bundled. The column lists
below must match it; `tests/telemetry/test_contract_schema.py` checks they do,
including that every `-- LOCAL` column is in `local_columns` and so never uploaded.
"""

import importlib.resources
from collections.abc import Mapping
from pathlib import Path

from speak.telemetry.identity import TelemetryIdentity
from speak.telemetry.schema import RowTable, TelemetrySchema

UTTERANCES = RowTable(
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
    # The contract's `-- LOCAL` columns: content and bookkeeping that never upload.
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
    # SQLite stores these as 0/1; the contract types them `boolean`.
    boolean_columns=("converted", "diarize"),
    seq_column="seq",
)

SCHEMA = TelemetrySchema(
    sql=importlib.resources.files("speak.telemetry").joinpath("schema.sql").read_text(),
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
    rows=UTTERANCES,
)

# Disclosure 2 is the first TELEMETRY.md that includes the install ping.
IDENTITY = TelemetryIdentity(
    plugin="speak",
    env_prefix="SPEAK",
    default_endpoint="https://speak-telemetry.actionableintelligence.app/v1/ingest",
    disclosure_version=2,
)

DB_FILENAME = "telemetry.db"


def db_path(env: Mapping[str, str], *, host_platform: str) -> Path:
    """The local store: in the config directory, beside consent."""
    return IDENTITY.config_dir(env, host_platform=host_platform) / DB_FILENAME
