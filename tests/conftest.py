import pytest


@pytest.fixture(autouse=True)
def _telemetry_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may write to the real machine's telemetry config directory.

    Telemetry consent is read from `os.environ` only by `__main__` and `talk`, the
    same two modules allowed to read `Settings` from it - a real subprocess test or
    a direct call into either one would otherwise touch this machine's actual
    `~/Library/Application Support/speak/`. A test that wants telemetry on sets its
    own environment explicitly, which overrides this default.
    """
    monkeypatch.setenv("SPEAK_TELEMETRY", "off")
