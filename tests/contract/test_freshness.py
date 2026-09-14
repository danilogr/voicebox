"""The vendored copy under `tests/contract/` must be byte-identical to the
`speak-telemetry` repo's `contract/`, or this client is testing against a contract
nobody else agrees to any more (spec 8: "vendors contract/v1/ into tests/contract/
with a checksum test that fails when the vendored copy goes stale").

Skipped, not failed, when the sibling repo is not checked out next to this one -
CI here may only have this repository, and that is not staleness.
"""

import hashlib
from pathlib import Path

import pytest

VENDORED_ROOT = Path(__file__).parent  # tests/contract/
UPSTREAM_ROOT = Path(__file__).resolve().parents[3] / "speak-telemetry" / "contract"


def _relative_vendored_files() -> list[Path]:
    files = [Path("CONTRACT.md")]
    files += sorted(
        p.relative_to(VENDORED_ROOT) for p in (VENDORED_ROOT / "v1").rglob("*") if p.is_file()
    )
    return files


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


pytestmark = pytest.mark.skipif(
    not UPSTREAM_ROOT.is_dir(),
    reason="speak-telemetry is not checked out beside this repo; cannot check freshness",
)


def test_the_vendored_contract_matches_the_upstream_repo() -> None:
    stale: list[str] = []
    for relative in _relative_vendored_files():
        vendored = VENDORED_ROOT / relative
        upstream = UPSTREAM_ROOT / relative
        if not upstream.exists():
            stale.append(f"{relative}: no longer exists upstream")
            continue
        if _sha256(vendored) != _sha256(upstream):
            stale.append(f"{relative}: content differs from upstream")
    assert not stale, (
        "vendored contract/v1/ has drifted from speak-telemetry/contract/ - "
        "re-copy it:\n  " + "\n  ".join(stale)
    )


def test_nothing_new_appeared_upstream_that_was_not_vendored() -> None:
    upstream_files = sorted(
        p.relative_to(UPSTREAM_ROOT.parent) for p in UPSTREAM_ROOT.rglob("*") if p.is_file()
    )
    vendored_files = {Path("contract") / f for f in _relative_vendored_files()}
    missing = [f for f in upstream_files if f not in vendored_files]
    assert not missing, f"upstream added files this repo has not vendored: {missing}"
