"""Both sides of the contract run the same fixtures (spec 8): every payload that
should validate does, and every payload that should not names the field CONTRACT.md
says a server built on ajv would name. This is what keeps the client's own
understanding of the wire format honest against the vendored schema.
"""

import json
from pathlib import Path

import pytest
from validate import validate_document

CONTRACT_DIR = Path(__file__).parent / "v1"
REQUEST_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ingest-request.schema.json").read_text())

VALID_DIR = CONTRACT_DIR / "fixtures" / "valid"
INVALID_DIR = CONTRACT_DIR / "fixtures" / "invalid"

VALID_FIXTURES = sorted(VALID_DIR.glob("*.json"))
INVALID_FIXTURES = sorted(
    p for p in INVALID_DIR.glob("*.json") if not p.name.endswith(".expected.json")
)


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_every_valid_fixture_validates(path: Path) -> None:
    instance = json.loads(path.read_text())
    errors = validate_document(REQUEST_SCHEMA, instance)
    assert errors == [], f"{path.name} should validate but got: {errors}"


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.stem)
def test_every_invalid_fixture_is_rejected_at_the_documented_field(path: Path) -> None:
    instance = json.loads(path.read_text())
    expected = json.loads(path.with_name(path.stem + ".expected.json").read_text())

    errors = validate_document(REQUEST_SCHEMA, instance)
    assert errors, f"{path.name} should be rejected but validated cleanly"
    assert any(error.pointer == expected["field"] for error in errors), (
        f"{path.name}: expected a violation at {expected['field']!r}, "
        f"got {[e.pointer for e in errors]}"
    )


def test_there_is_at_least_one_fixture_of_each_kind() -> None:
    """A directory that silently lost its fixtures would otherwise pass by having
    nothing to check."""
    assert len(VALID_FIXTURES) >= 8
    assert len(INVALID_FIXTURES) >= 8
