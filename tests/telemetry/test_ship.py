"""`ship.py`: projection, chunking, and the retry rules in CONTRACT.md. Every
transport here is injected - no test opens a socket."""

import json
from pathlib import Path

import pytest

from speak.telemetry import ship, store

INSTALL = {
    "install_id": "install-1",
    "first_seen_at": "2026-09-13T10:00:00Z",
    "last_seen_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "os": "darwin",
    "arch": "arm64",
    "accel": "metal",
    "country": None,  # the client never populates this; must never be uploaded
}

SESSION = {
    "session_id": "session-1",
    "install_id": "install-1",
    "started_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "listen_enabled": False,
    "harness": "claude-code",
}


def utterance(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "utterance_id": f"utterance-{overrides.get('seq', 1)}",
        "session_id": "session-1",
        "install_id": "install-1",
        "seq": 1,
        "direction": "out",
        "source": "speak",
        "occurred_at": "2026-09-13T10:00:01Z",
        "status": "ok",
        "text": "hi there",
        "text_sha256": "abc123",
        "device_name": "Danilo's AirPods Pro",
    }
    row.update(overrides)
    return row


def seeded_conn(rows: list[dict[str, object]]):
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    for row in rows:
        store.insert_utterance(conn, row)
    return conn


class FakeTransport:
    def __init__(self, responses: list[ship.TransportResponse] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = list(responses or [])

    async def __call__(self, url: str, body: bytes) -> ship.TransportResponse:
        payload = json.loads(body)
        self.calls.append((url, payload))
        if self._responses:
            return self._responses.pop(0)
        return ship.TransportResponse(status=202, body=b'{"accepted": 1}')


async def no_sleep(_: float) -> None:
    return None


def test_local_only_columns_are_never_in_the_projection() -> None:
    row = utterance()
    projected = ship._project_utterance(row)
    for column in ship.LOCAL_ONLY_UTTERANCE_COLUMNS:
        assert column not in projected


def test_country_is_never_in_the_install_projection() -> None:
    projected = ship._project_install({**INSTALL, "country": "BR"})
    assert "country" not in projected


def test_none_values_are_dropped_not_sent_as_null() -> None:
    projected = ship._project_utterance(utterance(engine=None, model=None))
    assert "engine" not in projected
    assert "model" not in projected


def test_listen_enabled_is_projected_as_a_real_boolean() -> None:
    projected = ship._project_session({**SESSION, "listen_enabled": 1})
    assert projected["listen_enabled"] is True


def test_chunks_split_at_100_rows() -> None:
    rows = [utterance(seq=n) for n in range(1, 151)]
    chunks = list(ship._chunks(rows))
    assert [len(c) for c in chunks] == [100, 50]


def test_chunks_split_before_the_byte_budget_is_exceeded(monkeypatch) -> None:
    monkeypatch.setattr(ship, "CHUNK_BYTES", 200)
    rows = [utterance(seq=n) for n in range(1, 6)]
    chunks = list(ship._chunks(rows))
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 5


async def test_ship_returns_zero_with_nothing_unshipped() -> None:
    conn = seeded_conn([])
    transport = FakeTransport()
    shipped = await ship.ship(conn, url="https://example/v1/ingest", transport=transport)
    assert shipped == 0
    assert transport.calls == []


async def test_a_successful_chunk_is_marked_shipped_and_never_sent_again() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 1
    assert store.unshipped_utterances(conn) == []


async def test_the_request_body_never_contains_a_local_only_column() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    await ship.ship(conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep)

    _, payload = transport.calls[0]
    for row in payload["utterances"]:
        for column in ship.LOCAL_ONLY_UTTERANCE_COLUMNS:
            assert column not in row


async def test_a_quota_shortfall_is_not_a_failure_and_the_chunk_still_ships() -> None:
    """CONTRACT.md: the per-install quota soft-drops. `accepted` under the number
    sent is normal, never retried, and the chunk is still marked shipped."""
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=202, body=b'{"accepted": 0}')])
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 1
    assert len(transport.calls) == 1  # no retry
    assert store.unshipped_utterances(conn) == []


async def test_a_400_is_not_retried_and_leaves_the_row_unshipped() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=400)])
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 0
    assert len(transport.calls) == 1
    assert len(store.unshipped_utterances(conn)) == 1


async def test_a_413_is_not_retried() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=413)])
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 0
    assert len(transport.calls) == 1


async def test_a_429_is_retried_with_backoff_and_then_succeeds() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport(
        [
            ship.TransportResponse(status=429),
            ship.TransportResponse(status=202, body=b'{"accepted": 1}'),
        ]
    )
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=record_sleep
    )
    assert shipped == 1
    assert len(transport.calls) == 2
    assert slept  # it waited, never a tight loop


async def test_a_5xx_is_retried() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport(
        [ship.TransportResponse(status=503), ship.TransportResponse(status=202, body=b"{}")]
    )
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 1


async def test_retries_give_up_after_a_fixed_number_of_attempts() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=503)] * 10)
    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep
    )
    assert shipped == 0
    assert len(transport.calls) == ship.MAX_ATTEMPTS


async def test_a_transport_failure_is_retried_then_gives_up(monkeypatch) -> None:
    conn = seeded_conn([utterance()])
    attempts: list[int] = []

    async def flaky(url: str, body: bytes) -> ship.TransportResponse:
        attempts.append(1)
        raise OSError("connection refused")

    shipped = await ship.ship(
        conn, url="https://example/v1/ingest", transport=flaky, sleep=no_sleep
    )
    assert shipped == 0
    assert len(attempts) == ship.MAX_ATTEMPTS


async def test_no_endpoint_configured_leaves_everything_unshipped() -> None:
    conn = seeded_conn([utterance()])
    shipped = await ship.ship(conn, url=None, transport=FakeTransport())
    assert shipped == 0
    assert len(store.unshipped_utterances(conn)) == 1


async def test_debug_mode_never_calls_the_transport_and_marks_nothing_shipped() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    shipped = await ship.ship(conn, url=None, transport=transport, debug=True)
    assert shipped == 0
    assert transport.calls == []
    assert len(store.unshipped_utterances(conn)) == 1


async def test_a_session_is_carried_only_in_the_chunk_that_first_needs_it(monkeypatch) -> None:
    monkeypatch.setattr(ship, "CHUNK_ROWS", 1)
    rows = [utterance(seq=1), utterance(seq=2)]
    conn = seeded_conn(rows)
    transport = FakeTransport(
        [
            ship.TransportResponse(status=202, body=b'{"accepted": 1}'),
            ship.TransportResponse(status=202, body=b'{"accepted": 1}'),
        ]
    )
    await ship.ship(conn, url="https://example/v1/ingest", transport=transport, sleep=no_sleep)

    first_sessions = transport.calls[0][1]["sessions"]
    second_sessions = transport.calls[1][1]["sessions"]
    assert len(first_sessions) == 1
    assert second_sessions == []


def test_bounds_not_given_by_the_spec_match_contract_md() -> None:
    """CONTRACT.md's own numbers (spec silent, contract fixes them): 100 rows and
    48 KB. A drift here is silent until a real chunk exceeds the server's cap."""
    assert ship.CHUNK_ROWS == 100
    assert ship.CHUNK_BYTES == 48_000


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_should_retry_matches_contract_md_exactly(status: int) -> None:
    assert ship._should_retry(status) is True


@pytest.mark.parametrize("status", [200, 202, 400, 404, 413])
def test_should_not_retry_these(status: int) -> None:
    assert ship._should_retry(status) is False


@pytest.mark.parametrize(("os_name", "arch"), [("darwin", "riscv64"), ("freebsd", "x86_64")])
async def test_an_install_outside_the_contracts_platforms_sends_nothing(os_name, arch) -> None:
    """`os` and `arch` are required and have no "other" value, so such an install
    would be refused on every chunk. Nothing is sent and nothing is marked."""
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, {**INSTALL, "os": os_name, "arch": arch})
    store.insert_session(conn, SESSION)
    store.insert_utterance(conn, utterance())
    transport = FakeTransport()

    shipped = await ship.ship(conn, url="https://example.test/v1/ingest", transport=transport)

    assert shipped == 0
    assert transport.calls == []
    assert len(store.unshipped_utterances(conn)) == 1
