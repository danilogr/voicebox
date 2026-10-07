import pytest


@pytest.fixture(autouse=True)
def _hermetic_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test may touch this machine's real voicebox state, telemetry or speech server.

    `__main__` reads `os.environ`, so a subprocess test or a direct call into it would
    otherwise write under the real `~/Library/Caches/voicebox` and
    `~/Library/Application Support/voicebox`, register with the developer's real speech
    server and take the real speaker lock. `DO_NOT_TRACK` is the one setting under which
    nothing is collected, nothing is sent and no file is written; a test that needs
    telemetry clears it and sets `VOICEBOX_TELEMETRY_DEBUG`, under which nothing is sent.
    """
    monkeypatch.setenv("VOICEBOX_TELEMETRY", "off")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("VOICEBOX_STATE_DIR", str(tmp_path_factory.mktemp("voicebox")))
    monkeypatch.setenv("SPEAK_CORE_STATE_DIR", str(tmp_path_factory.mktemp("speak-core")))
