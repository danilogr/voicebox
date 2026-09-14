"""Both sides of the contract run the same fixtures (spec 8): every payload that
should validate does, and every payload that should not names the field CONTRACT.md
says a server built on ajv would name. This runs the real `jsonschema` library,
not a reimplementation - a partial validator could pass a payload the server's ajv
would reject, which is worse than no client-side check at all.
"""

import json
import re
from pathlib import Path

import jsonschema
import pytest

CONTRACT_DIR = Path(__file__).parent / "v1"
REQUEST_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ingest-request.schema.json").read_text())

VALIDATOR = jsonschema.Draft202012Validator(REQUEST_SCHEMA)

VALID_DIR = CONTRACT_DIR / "fixtures" / "valid"
INVALID_DIR = CONTRACT_DIR / "fixtures" / "invalid"

VALID_FIXTURES = sorted(VALID_DIR.glob("*.json"))
INVALID_FIXTURES = sorted(
    p for p in INVALID_DIR.glob("*.json") if not p.name.endswith(".expected.json")
)


def _instance_path(error: jsonschema.ValidationError) -> str:
    """The RFC 6901 pointer to where `error` occurred, empty string at the root -
    matching CONTRACT.md's "a root-level ... violation has instancePath === ''"."""
    return "/" + "/".join(str(p) for p in error.absolute_path) if error.absolute_path else ""


def field_pointer(error: jsonschema.ValidationError) -> str:
    """CONTRACT.md's exact recipe for turning an ajv error into a `field` pointer,
    reproduced here against `jsonschema`'s differently-shaped errors for the same
    two keywords:

    - additionalProperties -> instancePath + "/" + the (one) disallowed property
    - required             -> instancePath + "/" + the missing property
    - every other keyword   -> instancePath unchanged
    """
    base = _instance_path(error)
    if error.validator == "additionalProperties":
        extra = re.findall(r"'([^']+)'", error.message)
        return f"{base}/{extra[0]}" if extra else base
    if error.validator == "required":
        missing = re.search(r"'([^']+)' is a required property", error.message)
        return f"{base}/{missing.group(1)}" if missing else base
    return base


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_every_valid_fixture_validates(path: Path) -> None:
    instance = json.loads(path.read_text())
    errors = list(VALIDATOR.iter_errors(instance))
    assert errors == [], f"{path.name} should validate but got: {[e.message for e in errors]}"


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.stem)
def test_every_invalid_fixture_is_rejected_at_the_documented_field(path: Path) -> None:
    instance = json.loads(path.read_text())
    expected = json.loads(path.with_name(path.stem + ".expected.json").read_text())

    errors = list(VALIDATOR.iter_errors(instance))
    assert errors, f"{path.name} should be rejected but validated cleanly"

    pointers = [field_pointer(e) for e in errors]
    field = expected["field"]
    if expected.get("field_is_prefix"):
        # JSON Schema does not specify how precisely a validator must locate an
        # error; CONTRACT.md marks this fixture's pointer as a prefix rather than
        # demanding an exact match no honest implementation is guaranteed to give.
        ok = any(pointer == field or pointer.startswith(field + "/") for pointer in pointers)
    else:
        ok = field in pointers
    assert ok, (
        f"{path.name}: expected a violation at {field!r} "
        f"({'as a prefix' if expected.get('field_is_prefix') else 'exactly'}), got {pointers} "
        f"(messages: {[e.message for e in errors]})"
    )


def test_there_is_at_least_one_fixture_of_each_kind() -> None:
    """A directory that silently lost its fixtures would otherwise pass by having
    nothing to check."""
    assert len(VALID_FIXTURES) >= 8
    assert len(INVALID_FIXTURES) >= 8
