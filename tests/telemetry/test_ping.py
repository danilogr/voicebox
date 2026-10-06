"""The install ping: when it is due, what it says, and that the marker moves only on
success. Every transport is injected - no test here opens a socket."""

import json
from pathlib import Path

import pytest
from telemetry_fixtures import IDENTITY

from speak.telemetry import consent, ping
from speak.telemetry.ship import TransportResponse

INGEST = "https://telemetry.example/v1/ingest"


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **extra}


def config_dir(tmp_path: Path) -> Path:
    return IDENTITY.config_dir(env(tmp_path), host_platform="linux")


def due(tmp_path: Path, version: str = "1.7.0", **extra: str) -> ping.Ping | None:
    return ping.due(
        env(tmp_path, **extra),
        IDENTITY,
        plugin_version=version,
        host_platform="linux",
        ingest_url=INGEST,
    )


class Transport:
    def __init__(self, response: TransportResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, url: str, body: bytes) -> TransportResponse:
        self.calls.append((url, json.loads(body)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


NO_CONTENT = TransportResponse(status=204)


async def test_a_first_run_pings_install_and_records_the_version(tmp_path):
    pending = due(tmp_path)
    assert pending is not None
    transport = Transport(NO_CONTENT)

    assert await ping.send(pending, transport=transport) is True

    ((url, payload),) = transport.calls
    assert url == "https://telemetry.example/v1/ping"
    assert payload == {"plugin_version": "1.7.0", "os": "linux", "event": "install"}
    marker = json.loads((config_dir(tmp_path) / ping.MARKER_FILENAME).read_text())
    assert marker == {"plugin_version": "1.7.0"}
    assert due(tmp_path) is None  # pinged once, not every session


def test_the_ping_carries_exactly_the_contracts_three_fields(tmp_path):
    """The body is identical in every mode and carries no identifier; a fourth field
    - an install id, the telemetry mode - would undo both."""
    pending = due(tmp_path)
    assert pending is not None
    assert set(pending.payload) == {"plugin_version", "os", "event"}


def test_an_install_that_predates_the_ping_reports_an_upgrade(tmp_path):
    """No marker, but a consent file: this machine ran an earlier version."""
    config_dir(tmp_path).mkdir(parents=True)
    (config_dir(tmp_path) / consent.CONFIG_FILENAME).write_text("{}")
    pending = due(tmp_path)
    assert pending is not None and pending.payload["event"] == "upgrade"


def test_a_new_version_reports_an_upgrade(tmp_path):
    config_dir(tmp_path).mkdir(parents=True)
    marker = config_dir(tmp_path) / ping.MARKER_FILENAME
    marker.write_text(json.dumps({"plugin_version": "1.6.0"}))
    pending = due(tmp_path, version="1.7.0")
    assert pending is not None and pending.payload["event"] == "upgrade"


def test_an_unreadable_marker_is_treated_as_no_marker(tmp_path):
    config_dir(tmp_path).mkdir(parents=True)
    (config_dir(tmp_path) / ping.MARKER_FILENAME).write_text("not json")
    pending = due(tmp_path)
    assert pending is not None and pending.payload["event"] == "install"


@pytest.mark.parametrize("mode", consent.MODES)
def test_the_ping_is_due_whatever_the_telemetry_mode(tmp_path, mode):
    """`off` and `local` still count the install; only DO_NOT_TRACK does not."""
    consent.ConsentStore(config_dir(tmp_path) / consent.CONFIG_FILENAME, disclosure_version=2).save(
        consent.Consent(install_id="x", disclosure_ver=2, mode=mode)
    )
    assert due(tmp_path) is not None


async def test_forcing_telemetry_off_creates_no_consent_file_and_still_pings(tmp_path):
    forced = env(tmp_path, SPEAK_TELEMETRY="off")
    pending = ping.due(
        forced, IDENTITY, plugin_version="1.7.0", host_platform="linux", ingest_url=INGEST
    )
    consent.load_consent(forced, IDENTITY, host_platform="linux")
    assert pending is not None
    await ping.send(pending, transport=Transport(NO_CONTENT))

    assert sorted(p.name for p in config_dir(tmp_path).iterdir()) == [ping.MARKER_FILENAME]


@pytest.mark.parametrize("value", ["1", "true"])
def test_do_not_track_sends_nothing_and_writes_nothing(tmp_path, value):
    assert due(tmp_path, DO_NOT_TRACK=value) is None
    assert not config_dir(tmp_path).exists()


async def test_debug_logs_the_ping_and_neither_sends_nor_records_it(tmp_path, caplog):
    pending = due(tmp_path)
    assert pending is not None
    transport = Transport(NO_CONTENT)
    with caplog.at_level("INFO", logger="speak.telemetry.ping"):
        assert await ping.send(pending, transport=transport, debug=True) is False
    assert transport.calls == []
    assert '"event": "install"' in caplog.text
    assert not config_dir(tmp_path).exists()


@pytest.mark.parametrize(
    "response",
    [
        TransportResponse(status=429),
        TransportResponse(status=500),
        TransportResponse(status=400),
        OSError("connection refused"),
    ],
    ids=["429", "500", "400", "unreachable"],
)
async def test_a_ping_that_is_not_counted_is_due_again(tmp_path, response):
    pending = due(tmp_path)
    assert pending is not None

    assert await ping.send(pending, transport=Transport(response)) is False

    assert not (config_dir(tmp_path) / ping.MARKER_FILENAME).exists()
    assert due(tmp_path) == pending


def test_an_os_the_contract_does_not_name_is_never_pinged(tmp_path):
    assert (
        ping.due(
            env(tmp_path),
            IDENTITY,
            plugin_version="1.7.0",
            host_platform="freebsd",
            ingest_url=INGEST,
        )
        is None
    )


@pytest.mark.parametrize(
    ("ingest", "expected"),
    [
        ("https://telemetry.example/v1/ingest", "https://telemetry.example/v1/ping"),
        ("http://localhost:8787/v1/ingest", "http://localhost:8787/v1/ping"),
        ("https://staging.example/some/prefix/v1/ingest", "https://staging.example/v1/ping"),
    ],
)
def test_the_ping_goes_to_the_ingest_endpoints_origin(ingest, expected):
    assert ping.ping_url(ingest) == expected
