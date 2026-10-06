"""`speak-core-setup`: the NeMo installer travels inside the package and is run with bash."""

from collections.abc import Sequence

from speak import setup


def test_the_installer_ships_inside_the_package():
    """Review focus: an install from git has no `scripts/` directory beside it."""
    assert setup.SCRIPT.is_file()
    assert setup.SCRIPT.read_text().startswith("#!/usr/bin/env bash")


def test_run_hands_the_packaged_script_to_bash_and_returns_its_status():
    seen: list[list[str]] = []

    def runner(argv: Sequence[str]) -> int:
        seen.append(list(argv))
        return 3

    assert setup.run(runner=runner) == 3
    assert seen == [["bash", str(setup.SCRIPT)]]


def test_the_installer_reads_the_shared_unprefixed_variables():
    text = setup.SCRIPT.read_text()
    assert '"${NEMO_BIN:-nemo-speech}"' in text
    assert "NEMO_INSTALLER" in text
    assert "SPEAK_" not in text
