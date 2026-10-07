"""voicebox's telemetry contract as speak-core's store and shipper need it.

The schema is `voicebox-telemetry`'s `contract/v1/schema.sql`, bundled byte for byte.
The column lists below must match it; `tests/telemetry/test_contract_schema.py` checks
that they do, including that every `-- LOCAL` column is local and so never uploaded.
"""

import importlib.resources
from pathlib import Path

from speak_core.telemetry.identity import TelemetryIdentity
from speak_core.telemetry.schema import RowTable, TelemetrySchema

JOBS = RowTable(
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
        "wall_ms",
        "status",
        "reason",
        "stage",
        "engine",
        "model",
        "server_warm",
        "audio_ms",
        "chars",
        "words",
        "format",
        "voice",
        "synth_ms",
        "source_bytes",
        "audio_codec",
        "has_video",
        "converted",
        "convert_ms",
        "asr_ms",
        "diarize",
        "speakers",
        "language",
        "translate",
        "input_kind",
        "input_format",
        "paragraphs",
        "chunks",
        "chunks_done",
        "encode_ms",
        "output_bytes",
        "sentences",
        "sentences_played",
        "fallback_sentences",
        "error_message",
        "source_name",
        "output_path",
        "shipped_at",
        "rejected_at",
    ),
    # The contract's `-- LOCAL` columns: paths, names and bookkeeping that never upload.
    local_columns=frozenset(
        {"error_message", "source_name", "output_path", "shipped_at", "rejected_at"}
    ),
    # SQLite stores these as 0/1; the contract types them `boolean`.
    boolean_columns=("server_warm", "has_video", "converted", "diarize", "translate"),
)

SCHEMA = TelemetrySchema(
    sql=importlib.resources.files("voicebox.telemetry").joinpath("schema.sql").read_text(),
    session_columns=(
        "session_id",
        "install_id",
        "started_at",
        "ended_at",
        "plugin_version",
        "core_version",
        "nemo_version",
        "harness",
        "harness_version",
        "ffmpeg_present",
    ),
    session_boolean_columns=("ffmpeg_present",),
    rows=JOBS,
)

# Disclosure 1 is the first TELEMETRY.md and first-run notice voicebox ships.
IDENTITY = TelemetryIdentity(
    plugin="voicebox",
    env_prefix="VOICEBOX",
    default_endpoint="https://voicebox-telemetry.actionableintelligence.app/v1/ingest",
    disclosure_version=1,
)

DB_FILENAME = "telemetry.db"


def db_path(state_dir: Path) -> Path:
    """The local store, in the plugin's state dir; consent stays in the config dir."""
    return state_dir / DB_FILENAME
