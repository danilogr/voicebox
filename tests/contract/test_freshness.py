"""The vendored copy must be byte-identical to `voicebox-telemetry/contract/`, or this
client tests against a contract nobody else agrees to any more. Skipped, not failed,
when that repo is not checked out beside this one."""

import hashlib
from pathlib import Path

import pytest

VENDORED_ROOT = Path(__file__).parent
UPSTREAM_ROOT = Path(__file__).resolve().parents[3] / "voicebox-telemetry" / "contract"
RUNTIME_SCHEMA = (
    Path(__file__).resolve().parents[2] / "src" / "voicebox" / "telemetry" / "schema.sql"
)

pytestmark = pytest.mark.skipif(
    not UPSTREAM_ROOT.is_dir(), reason="voicebox-telemetry is not checked out beside this repo"
)


def _vendored() -> list[Path]:
    return [
        Path("CONTRACT.md"),
        *sorted(
            p.relative_to(VENDORED_ROOT) for p in (VENDORED_ROOT / "v1").rglob("*") if p.is_file()
        ),
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_vendored_contract_matches_upstream() -> None:
    stale = [
        str(relative)
        for relative in _vendored()
        if not (UPSTREAM_ROOT / relative).exists()
        or _digest(VENDORED_ROOT / relative) != _digest(UPSTREAM_ROOT / relative)
    ]
    assert not stale, f"re-copy these from voicebox-telemetry/contract/: {stale}"


def test_nothing_new_appeared_upstream() -> None:
    upstream = {p.relative_to(UPSTREAM_ROOT) for p in UPSTREAM_ROOT.rglob("*") if p.is_file()}
    missing = sorted(str(p) for p in upstream - set(_vendored()))
    assert not missing, f"upstream added files this repo has not vendored: {missing}"
