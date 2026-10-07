"""Both sides of the contract run the same fixtures: every payload that should validate
does, and every payload that should not names the field CONTRACT.md says a server built
on ajv would name. This runs the real `jsonschema` library: a partial validator could
pass a payload the server's ajv would reject, which is worse than no check at all.
"""

import json
import re
from pathlib import Path

import jsonschema
import pytest

CONTRACT_DIR = Path(__file__).parent / "v1"
SCHEMAS = CONTRACT_DIR / "schema"
REQUEST = jsonschema.Draft202012Validator(
    json.loads((SCHEMAS / "ingest-request.schema.json").read_text())
)
PING = jsonschema.Draft202012Validator(
    json.loads((SCHEMAS / "ping-request.schema.json").read_text())
)
FIXTURES = CONTRACT_DIR / "fixtures"


def bodies(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.json") if not p.name.endswith(".expected.json"))


CASES = [
    *((REQUEST, p, True) for p in bodies(FIXTURES / "valid")),
    *((REQUEST, p, False) for p in bodies(FIXTURES / "invalid")),
    *((PING, p, True) for p in bodies(FIXTURES / "ping" / "valid")),
    *((PING, p, False) for p in bodies(FIXTURES / "ping" / "invalid")),
]


def field_pointer(error: jsonschema.ValidationError) -> str:
    """CONTRACT.md's recipe for an ajv error's pointer, applied to `jsonschema`'s errors."""
    base = "/" + "/".join(str(p) for p in error.absolute_path) if error.absolute_path else ""
    if error.validator == "additionalProperties":
        extra = re.findall(r"'([^']+)'", error.message)
        return f"{base}/{extra[0]}" if extra else base
    if error.validator == "required":
        missing = re.search(r"'([^']+)' is a required property", error.message)
        return f"{base}/{missing.group(1)}" if missing else base
    return base


@pytest.mark.parametrize(
    ("validator", "path", "valid"), CASES, ids=lambda c: getattr(c, "stem", "")
)
def test_every_fixture_behaves_as_documented(validator, path: Path, valid: bool) -> None:
    errors = list(validator.iter_errors(json.loads(path.read_text())))
    if valid:
        assert errors == [], [e.message for e in errors]
        return
    expected = json.loads(path.with_name(path.stem + ".expected.json").read_text())
    pointers = [field_pointer(e) for e in errors]
    field = expected["field"]
    if expected.get("field_is_prefix"):
        assert any(p == field or p.startswith(field + "/") for p in pointers), pointers
    else:
        assert field in pointers, pointers


def test_the_fixture_counts_are_the_ones_contract_md_states():
    """A directory that silently lost fixtures would otherwise pass by checking nothing.
    The counts are read from the vendored CONTRACT.md's "Fixtures" paragraph."""
    text = " ".join((CONTRACT_DIR.parent / "CONTRACT.md").read_text().split())
    bodies_stated = re.search(r"holds (\d+) `valid/` bodies and (\d+) `invalid/` bodies", text)
    pings_stated = re.search(r"`fixtures/ping/` holds (\d+) `valid/` and (\d+) `invalid/`", text)
    assert bodies_stated and pings_stated, "CONTRACT.md no longer states the fixture counts"
    assert len(bodies(FIXTURES / "valid")) == int(bodies_stated.group(1))
    assert len(bodies(FIXTURES / "invalid")) == int(bodies_stated.group(2))
    assert len(bodies(FIXTURES / "ping" / "valid")) == int(pings_stated.group(1))
    assert len(bodies(FIXTURES / "ping" / "invalid")) == int(pings_stated.group(2))
