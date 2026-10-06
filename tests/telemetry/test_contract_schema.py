"""This plugin's telemetry contract as the code declares it must be the contract the
schema says: a column missing from `local_columns` would be uploaded."""

import re
from pathlib import Path

from speak.telemetry import store
from speak.telemetry.contract import IDENTITY, SCHEMA

VENDORED = Path(__file__).parent.parent / "contract" / "v1" / "schema.sql"


def test_the_runtime_schema_matches_the_vendored_contract_copy() -> None:
    assert VENDORED.read_text() == SCHEMA.sql


def columns(table: str) -> tuple[str, ...]:
    conn = store.connect(Path(":memory:"), SCHEMA)
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def test_the_declared_columns_are_the_tables_columns() -> None:
    assert columns("utterances") == SCHEMA.rows.columns
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


def test_every_column_the_contract_tags_local_stays_local() -> None:
    assert local_columns("utterances") == SCHEMA.rows.local_columns
    assert local_columns("sessions") == {"shipped_at"}


def test_no_uploaded_session_column_is_local() -> None:
    assert not local_columns("sessions") & set(SCHEMA.session_columns)


def test_the_endpoint_and_disclosure_version_are_the_contracts() -> None:
    assert IDENTITY.default_endpoint == (
        "https://speak-telemetry.actionableintelligence.app/v1/ingest"
    )
    assert IDENTITY.disclosure_version == 2
    assert IDENTITY.mode_env == "SPEAK_TELEMETRY"
