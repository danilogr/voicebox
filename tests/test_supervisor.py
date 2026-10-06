import json
import os
import signal
import time

import pytest

from speak import supervisor as supervisor_module
from speak.config import ServerSettings
from speak.runtime import supervisor_for
from speak.supervisor import NemoSupervisor


class Spawns:
    def __init__(self, pid: int = 4242) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._pid = pid

    async def __call__(self, binary: str, args) -> int:
        self.calls.append((binary, list(args)))
        return self._pid


async def probe_ok() -> bool:
    return True


class Probes:
    """Counts how often the supervisor waited for the server to answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


def build(tmp_path, **kwargs: object) -> NemoSupervisor:
    defaults = dict(
        state_dir=tmp_path,
        spawn=Spawns(),
        probe=probe_ok,
        pid=1000,
        is_alive=lambda pid: True,
        kill=lambda pid, sig: None,
    )
    return NemoSupervisor(**{**defaults, **kwargs})


async def test_register_leaves_a_file_named_for_this_process(tmp_path):
    await build(tmp_path, pid=1234).register()
    assert (tmp_path / "clients" / "1234").exists()


async def test_register_is_idempotent(tmp_path):
    supervisor = build(tmp_path, pid=1234)
    await supervisor.register()
    await supervisor.register()
    assert [p.name for p in (tmp_path / "clients").iterdir()] == ["1234"]


async def test_ensure_running_spawns_the_server_with_auto_device_and_the_model(tmp_path):
    spawns = Spawns()
    assert await build(tmp_path, spawn=spawns, model="magpie", port=8082).ensure_running() is True

    binary, args = spawns.calls[0]
    assert binary == "nemo-speech"
    assert args == [
        "serve",
        "--tts-model",
        "magpie",
        "--port",
        "8082",
        "--device",
        "auto",
        "--no-ui",
    ]


async def test_ensure_running_passes_the_seed_only_when_it_is_set(tmp_path):
    spawns = Spawns()
    await build(tmp_path, spawn=spawns, seed=7).ensure_running()
    assert spawns.calls[0][1][-2:] == ["--tts.seed", "7"]


async def test_ensure_running_records_the_server_pid_and_port(tmp_path):
    await build(tmp_path, spawn=Spawns(pid=999), port=9001).ensure_running()
    recorded = json.loads((tmp_path / "server.json").read_text())
    assert recorded["pid"] == 999
    assert recorded["port"] == 9001


async def test_ensure_running_releases_the_lock_when_it_finishes(tmp_path):
    await build(tmp_path).ensure_running()
    assert not (tmp_path / "starting").exists()


async def test_a_server_that_is_already_running_is_not_replaced(tmp_path):
    """The TTS fallback calls this on any HTTP failure, a 5xx from a busy server included.

    A second `nemo-speech` cannot bind the port, so spawning would only overwrite the
    record of the server that is still answering and orphan it.
    """
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    spawns = Spawns(pid=9999)

    supervisor = build(tmp_path, spawn=spawns, is_alive=lambda pid: pid == 4242)

    assert await supervisor.ensure_running() is False
    assert spawns.calls == []
    assert json.loads((tmp_path / "server.json").read_text())["pid"] == 4242


async def test_a_server_another_session_is_starting_is_waited_for(tmp_path):
    """The caller's next move is to transcribe against it. Returning the moment the
    record exists hands back a server that has not bound its port, and the call site
    then reports a missing transcription model for a several-second cold start."""
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    probes = Probes()

    supervisor = build(tmp_path, probe=probes, is_alive=lambda pid: pid == 4242)

    assert await supervisor.ensure_running() is False
    assert probes.calls == 1


async def test_a_session_that_loses_the_start_race_waits_for_the_winner(tmp_path):
    (tmp_path / "starting").mkdir(parents=True)
    probes = Probes()

    assert await build(tmp_path, probe=probes).ensure_running() is False
    assert probes.calls == 1


async def test_a_recorded_server_that_has_died_is_replaced(tmp_path):
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    spawns = Spawns(pid=9999)

    supervisor = build(tmp_path, spawn=spawns, is_alive=lambda pid: False)

    assert await supervisor.ensure_running() is True
    assert json.loads((tmp_path / "server.json").read_text())["pid"] == 9999


async def test_an_unreadable_server_record_does_not_block_a_start(tmp_path):
    (tmp_path / "server.json").write_text("half a json file")
    spawns = Spawns(pid=9999)

    assert await build(tmp_path, spawn=spawns).ensure_running() is True
    assert len(spawns.calls) == 1


async def test_only_one_session_wins_the_right_to_start(tmp_path):
    (tmp_path / "starting").mkdir(parents=True)
    spawns = Spawns()
    assert await build(tmp_path, spawn=spawns).ensure_running() is False
    assert spawns.calls == []


async def test_a_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "starting"
    lock.mkdir(parents=True)
    long_ago = time.time() - 3600
    os.utime(lock, (long_ago, long_ago))

    spawns = Spawns()
    assert await build(tmp_path, spawn=spawns).ensure_running() is True
    assert len(spawns.calls) == 1


async def test_the_lock_is_released_even_when_the_probe_fails(tmp_path):
    async def probe_explodes() -> bool:
        raise TimeoutError("server never answered")

    with pytest.raises(TimeoutError):
        await build(tmp_path, probe=probe_explodes).ensure_running()
    assert not (tmp_path / "starting").exists()


async def test_release_keeps_the_server_while_another_session_is_alive(tmp_path):
    killed: list[int] = []
    supervisor = build(tmp_path, pid=1000, kill=lambda pid, sig: killed.append(pid))
    await supervisor.register()
    (tmp_path / "clients" / "2000").write_text("")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))

    assert await supervisor.release() is False
    assert killed == []


async def test_release_stops_the_server_when_the_last_session_leaves(tmp_path):
    killed: list[int] = []
    supervisor = build(tmp_path, pid=1000, kill=lambda pid, sig: killed.append(pid))
    await supervisor.register()
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))

    assert await supervisor.release() is True
    assert killed == [4242]
    assert not (tmp_path / "server.json").exists()


async def test_release_prunes_sessions_that_died_without_releasing(tmp_path):
    killed: list[int] = []
    supervisor = build(
        tmp_path,
        pid=1000,
        is_alive=lambda pid: pid != 2000,
        kill=lambda pid, sig: killed.append(pid),
    )
    await supervisor.register()
    (tmp_path / "clients" / "2000").write_text("")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))

    assert await supervisor.release() is True
    assert killed == [4242]
    assert not (tmp_path / "clients" / "2000").exists()


async def test_release_discards_client_files_that_are_not_pids(tmp_path):
    supervisor = build(tmp_path, pid=1000)
    await supervisor.register()
    (tmp_path / "clients" / ".DS_Store").write_text("")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    assert await supervisor.release() is True


async def test_release_without_a_recorded_server_does_nothing(tmp_path):
    supervisor = build(tmp_path, pid=1000)
    await supervisor.register()
    assert await supervisor.release() is False


async def test_release_survives_a_server_that_is_already_gone(tmp_path):
    def kill_missing(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    supervisor = build(tmp_path, pid=1000, kill=kill_missing)
    await supervisor.register()
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    assert await supervisor.release() is True
    assert not (tmp_path / "server.json").exists()


async def test_release_before_register_is_harmless(tmp_path):
    assert await build(tmp_path).release() is False


async def test_no_asr_model_is_served_unless_one_is_configured(tmp_path):
    spawns = Spawns()
    await build(tmp_path, spawn=spawns).ensure_running()
    _binary, args = spawns.calls[0]
    assert "--asr-model" not in args


async def test_the_configured_asr_model_is_served_alongside_speech(tmp_path):
    spawns = Spawns()
    await build(tmp_path, spawn=spawns, asr_model="nemotron-3.5").ensure_running()
    _binary, args = spawns.calls[0]
    assert args[args.index("--asr-model") + 1] == "nemotron-3.5"
    assert "--tts-model" in args


@pytest.mark.parametrize(
    "written",
    ['{"port": 8082}', "[1, 2]", '"8082"', "null", '{"pid": "4242"}'],
    ids=["no-pid", "a-list", "a-string", "null", "pid-not-an-int"],
)
async def test_release_survives_a_server_record_it_did_not_write(tmp_path, written):
    """`release` runs in a `finally` whose caller catches a fixed set of exceptions, so
    anything else raised there reaches the user as a traceback - and on the /talk path
    a traceback lands where the user's own words are read from. The file is on disk,
    so its shape is not this code's to assume: it parsed, which is not the same thing."""
    (tmp_path / "server.json").write_text(written)
    supervisor = build(tmp_path)
    await supervisor.register()

    assert await supervisor.release() is False


async def test_release_still_stops_a_server_it_can_read(tmp_path):
    """The guard above must not have turned every release into a no-op."""
    killed: list[tuple[int, int]] = []
    (tmp_path / "server.json").write_text(json.dumps({"pid": 4242, "port": 8082}))
    supervisor = build(tmp_path, kill=lambda pid, sig: killed.append((pid, sig)))
    await supervisor.register()

    assert await supervisor.release() is True
    assert [pid for pid, _signal in killed] == [4242]


async def test_a_stale_lock_that_cannot_be_removed_gives_up(tmp_path, monkeypatch):
    """`rmtree` ignores its errors, so a lock directory that cannot be deleted leaves
    the mtime stale forever. Retrying that without a bound exhausts the stack, and
    RecursionError is not in the set of exceptions the callers catch."""
    lock = tmp_path / "starting"
    lock.mkdir(parents=True)
    long_ago = time.time() - 3600
    os.utime(lock, (long_ago, long_ago))
    monkeypatch.setattr("speak.supervisor.shutil.rmtree", lambda *_a, **_k: None)

    spawns = Spawns()
    assert await build(tmp_path, spawn=spawns).ensure_running() is False
    assert spawns.calls == []


class FlakyProbe:
    """Answers False until `succeed_from` calls have been made, then True.

    Models a server that spawns and then dies because a model would not load: the
    process starts, the port never answers.
    """

    def __init__(self, succeed_from: int = 2) -> None:
        self.calls = 0
        self._succeed_from = succeed_from

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self._succeed_from


async def test_a_server_that_will_not_start_with_asr_is_retried_without_it(tmp_path):
    """A missing transcription model must cost listening, never speech.

    Voice input is unconditional, so every session now asks for an ASR model. A user
    who installed before the setup script pulled one would otherwise lose speech too.
    """
    spawns = Spawns()
    # A server that could not load its model has exited by the time it is signalled.
    supervisor = build(
        tmp_path,
        spawn=spawns,
        probe=FlakyProbe(),
        asr_model="nemotron-3.5",
        is_alive=lambda pid: False,
    )

    assert await supervisor.ensure_running() is True

    assert len(spawns.calls) == 2, "expected a second attempt without the ASR model"
    first, second = (args for _, args in spawns.calls)
    assert "--asr-model" in first
    assert "--asr-model" not in second
    assert "--tts-model" in second, "speech must survive the fallback"


async def test_a_server_that_starts_first_time_is_not_respawned(tmp_path):
    spawns = Spawns()
    supervisor = build(tmp_path, spawn=spawns, probe=probe_ok, asr_model="nemotron-3.5")

    assert await supervisor.ensure_running() is True

    assert len(spawns.calls) == 1
    assert "--asr-model" in spawns.calls[0][1]


async def test_the_fallback_records_the_server_that_actually_answered(tmp_path):
    """`server.json` drives shutdown, so it must name the surviving process."""
    supervisor = build(
        tmp_path,
        spawn=Spawns(pid=777),
        probe=FlakyProbe(),
        asr_model="nemotron-3.5",
        is_alive=lambda pid: False,
    )
    await supervisor.ensure_running()
    assert json.loads((tmp_path / "server.json").read_text())["pid"] == 777


async def test_a_speech_only_server_is_never_retried(tmp_path):
    """Without an ASR model there is nothing to drop, so a dead server stays dead."""
    spawns = Spawns()
    supervisor = build(tmp_path, spawn=spawns, probe=FlakyProbe(succeed_from=99))

    await supervisor.ensure_running()

    assert len(spawns.calls) == 1


class DyingProcess:
    """Alive until `dies_after` liveness checks, then gone.

    SIGTERM is asynchronous: the process the supervisor just signalled still holds
    the port for a moment, and the retry binds that same port.
    """

    def __init__(self, dies_after: int = 2) -> None:
        self.checks = 0
        self.signalled: list[int] = []
        self._dies_after = dies_after

    def is_alive(self, pid: int) -> bool:
        self.checks += 1
        return self.checks < self._dies_after

    def kill(self, pid: int, sig: int) -> None:
        self.signalled.append(pid)


async def test_the_retry_waits_for_the_failed_attempt_to_actually_exit(tmp_path):
    """Respawning while the old process still holds the port could not bind it."""
    process = DyingProcess()
    supervisor = build(
        tmp_path,
        spawn=Spawns(),
        probe=FlakyProbe(),
        asr_model="nemotron-3.5",
        is_alive=process.is_alive,
        kill=process.kill,
    )
    # A live recorded server would short-circuit before any spawn is attempted.
    supervisor._recorded_server_is_alive = lambda: False  # type: ignore[method-assign]

    await supervisor.ensure_running()

    assert process.signalled, "the failed attempt was never signalled"
    assert process.checks >= 1, "the retry did not wait for the old process to exit"


async def test_a_failed_attempt_that_will_not_die_does_not_hang_the_session(tmp_path, monkeypatch):
    """Waiting forever for a wedged process would be worse than a retry that fails."""
    monkeypatch.setattr(supervisor_module, "TERMINATE_TIMEOUT", 0.05)
    process = DyingProcess(dies_after=10_000)
    supervisor = build(
        tmp_path,
        spawn=Spawns(),
        probe=FlakyProbe(),
        asr_model="nemotron-3.5",
        is_alive=process.is_alive,
        kill=process.kill,
    )
    supervisor._recorded_server_is_alive = lambda: False  # type: ignore[method-assign]

    assert await supervisor.ensure_running() is True


async def test_two_plugins_elect_one_server_through_the_shared_directory(tmp_path):
    """walkie and voicebox build their supervisors from their own environments; the
    shared directory is the same for both, so the second finds the first's server."""
    home = tmp_path / "home"
    walkie_env = {"HOME": str(home), "WALKIE_ENGINE": "say", "WALKIE_VOICE": "Samantha"}
    voicebox_env = {"HOME": str(home), "VOICEBOX_ENGINE": "nemo", "VOICEBOX_VOICE": "Sofia"}
    spawns = Spawns(pid=4242)
    kills: list[tuple[int, int]] = []
    alive = {1001, 1002, 4242}

    def build_for(env: dict[str, str], pid: int) -> NemoSupervisor:
        return supervisor_for(
            ServerSettings.from_env(env, platform="darwin"),
            spawn=spawns,
            probe=probe_ok,
            pid=pid,
            is_alive=alive.__contains__,
            kill=lambda target, sig: kills.append((target, sig)),
        )

    walkie, voicebox = build_for(walkie_env, 1001), build_for(voicebox_env, 1002)
    await walkie.register()
    await voicebox.register()

    started = [await walkie.ensure_running(), await voicebox.ensure_running()]

    assert started == [True, False]
    assert len(spawns.calls) == 1
    shared = home / "Library" / "Caches" / "speak-core"
    assert json.loads((shared / "server.json").read_text())["pid"] == 4242
    assert {entry.name for entry in (shared / "clients").iterdir()} == {"1001", "1002"}
    assert await walkie.release() is False
    assert await voicebox.release() is True
    assert kills == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize(
    "plugin_env",
    [
        {},
        {"WALKIE_ENGINE": "say", "WALKIE_VOICE": "Samantha", "SPEAK_SEED": "1"},
        {"VOICEBOX_ENGINE": "nemo", "VOICEBOX_VOICE": "Sofia", "VOICEBOX_STATE_DIR": "/elsewhere"},
    ],
)
async def test_server_arguments_depend_only_on_the_shared_settings(tmp_path, plugin_env):
    """A plugin's own variables, including a stale SPEAK_SEED, never reach the server."""
    shared = {
        "HOME": str(tmp_path),
        "NEMO_HTTP_URL": "http://127.0.0.1:8090",
        "NEMO_TTS_MODEL": "magpie",
        "NEMO_ASR_MODEL": "nemotron-3.5",
        "NEMO_TTS_SEED": "7",
        "NEMO_BIN": "/opt/nemo",
    }
    spawns = Spawns()
    supervisor = supervisor_for(
        ServerSettings.from_env({**shared, **plugin_env}, platform="linux"),
        spawn=spawns,
        probe=probe_ok,
        pid=1,
        is_alive=lambda pid: False,
    )

    await supervisor.ensure_running()

    assert spawns.calls == [
        (
            "/opt/nemo",
            [
                "serve",
                "--tts-model",
                "magpie",
                "--port",
                "8090",
                "--device",
                "auto",
                "--no-ui",
                "--asr-model",
                "nemotron-3.5",
                "--tts.seed",
                "7",
            ],
        )
    ]
