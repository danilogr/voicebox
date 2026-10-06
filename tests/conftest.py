import pytest


@pytest.fixture(autouse=True)
def _telemetry_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test may write to the real machine's telemetry config directory.

    Telemetry consent is read from `os.environ` only by `__main__` and `talk`, the
    same two modules allowed to read `Settings` from it - a real subprocess test or
    a direct call into either one would otherwise touch this machine's actual
    `~/Library/Application Support/speak/`. A test that wants telemetry on sets its
    own environment explicitly, which overrides this default.

    `off` still sends the install ping, so `DO_NOT_TRACK` is set as well: it is the
    one setting under which neither entry point makes a request or writes a file.
    A test that needs either one running clears it and sets `SPEAK_TELEMETRY_DEBUG`,
    under which nothing is sent.

    The shared state directory is pinned to a temporary one for the same reason: a
    subprocess test would otherwise register with the developer's real speech server
    and take the real speaker lock.
    """
    monkeypatch.setenv("SPEAK_TELEMETRY", "off")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("SPEAK_CORE_STATE_DIR", str(tmp_path_factory.mktemp("speak-core")))
