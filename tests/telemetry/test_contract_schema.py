"""voicebox's telemetry contract as the code declares it must be the one the schema
says: a column missing from `local_columns` would be uploaded."""

import re
from pathlib import Path

from speak_core.telemetry import store

from voicebox.telemetry.contract import IDENTITY, SCHEMA, db_path

VENDORED = Path(__file__).parents[1] / "contract" / "v1" / "schema.sql"


def test_the_runtime_schema_is_the_vendored_contract_byte_for_byte():
    assert SCHEMA.sql == VENDORED.read_text()


def columns(table: str) -> tuple[str, ...]:
    conn = store.connect(Path(":memory:"), SCHEMA)
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def test_the_declared_columns_are_the_tables_columns():
    assert columns("jobs") == SCHEMA.rows.columns
    assert columns("sessions") == (*SCHEMA.session_columns, "shipped_at")


def local_columns(table: str) -> set[str]:
    current, found = None, set()
    for line in SCHEMA.sql.splitlines():
        started = re.match(r"CREATE TABLE (\w+)", line)
        if started:
            current = started.group(1)
            continue
        column = re.match(r"\s+(\w+)\s+\w+", line)
        if current == table and column and "-- LOCAL" in line:
            found.add(column.group(1))
    return found


def test_every_column_the_contract_tags_local_stays_local():
    assert local_columns("jobs") == SCHEMA.rows.local_columns
    assert local_columns("sessions") == {"shipped_at"}
    assert not local_columns("sessions") & set(SCHEMA.session_columns)


def test_the_boolean_columns_are_the_contracts_booleans():
    assert set(SCHEMA.rows.boolean_columns) == {
        "server_warm",
        "has_video",
        "converted",
        "diarize",
        "translate",
    }
    assert SCHEMA.session_boolean_columns == ("ffmpeg_present",)


def test_the_identity_is_voiceboxs():
    assert IDENTITY.plugin == "voicebox"
    assert IDENTITY.default_endpoint == (
        "https://voicebox-telemetry.actionableintelligence.app/v1/ingest"
    )
    assert IDENTITY.disclosure_version == 1
    assert (IDENTITY.mode_env, IDENTITY.debug_env, IDENTITY.endpoint_env, IDENTITY.harness_env) == (
        "VOICEBOX_TELEMETRY",
        "VOICEBOX_TELEMETRY_DEBUG",
        "VOICEBOX_TELEMETRY_ENDPOINT",
        "VOICEBOX_HARNESS",
    )


def test_the_store_lives_in_the_plugins_state_dir(tmp_path):
    assert db_path(tmp_path) == tmp_path / "telemetry.db"
