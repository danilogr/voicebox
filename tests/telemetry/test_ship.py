"""`ship.py`: projection, chunking, and the retry rules in CONTRACT.md. Every
transport here is injected - no test opens a socket."""

import json
from pathlib import Path

import pytest
from telemetry_fixtures import UTTERANCES

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
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)
    for row in rows:
        store.insert_row(conn, UTTERANCES.rows, row)
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
    projected = ship.project_row(UTTERANCES.rows, row)
    for column in UTTERANCES.rows.local_columns:
        assert column not in projected


def test_country_is_never_in_the_install_projection() -> None:
    projected = ship.project_install({**INSTALL, "country": "BR"})
    assert "country" not in projected


def test_none_values_are_dropped_not_sent_as_null() -> None:
    projected = ship.project_row(UTTERANCES.rows, utterance(engine=None, model=None))
    assert "engine" not in projected
    assert "model" not in projected


def test_listen_enabled_is_projected_as_a_real_boolean() -> None:
    projected = ship.project_session(UTTERANCES, {**SESSION, "listen_enabled": 1})
    assert projected["listen_enabled"] is True


def test_chunks_split_at_100_rows() -> None:
    rows = [utterance(seq=n) for n in range(1, 151)]
    chunks = list(ship._chunks(rows, {}, UTTERANCES))
    assert [len(c) for c in chunks] == [100, 50]


def test_chunks_split_before_the_byte_budget_is_exceeded(monkeypatch) -> None:
    monkeypatch.setattr(ship, "CHUNK_BYTES", 200)
    rows = [utterance(seq=n) for n in range(1, 6)]
    chunks = list(ship._chunks(rows, {}, UTTERANCES))
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 5


async def test_ship_returns_zero_with_nothing_unshipped() -> None:
    conn = seeded_conn([])
    store.mark_sessions_shipped(conn, store.unshipped_sessions(conn, "install-1"), "t")
    transport = FakeTransport()
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
    )
    assert shipped == 0
    assert transport.calls == []


async def test_a_successful_chunk_is_marked_shipped_and_never_sent_again() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )
    assert shipped == 1
    assert store.unshipped_rows(conn, UTTERANCES.rows) == []


async def test_the_request_body_never_contains_a_local_only_column() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )

    _, payload = transport.calls[0]
    for row in payload["utterances"]:
        for column in UTTERANCES.rows.local_columns:
            assert column not in row


async def test_a_quota_shortfall_is_not_a_failure_and_the_chunk_still_ships() -> None:
    """CONTRACT.md: the per-install quota soft-drops. `accepted` under the number
    sent is normal, never retried, and the chunk is still marked shipped."""
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=202, body=b'{"accepted": 0}')])
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )
    assert shipped == 1
    assert len(transport.calls) == 1  # no retry
    assert store.unshipped_rows(conn, UTTERANCES.rows) == []


async def test_a_400_is_not_retried_and_sets_the_chunk_aside() -> None:
    """The payload is the problem, so resending it cannot succeed. Left in the
    backlog it would be resent first every session, ahead of every good row."""
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=400)])
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )
    assert shipped == 0
    assert [len(payload["utterances"]) for _, payload in transport.calls] == [1, 0]
    assert store.unshipped_rows(conn, UTTERANCES.rows) == []
    (row,) = conn.execute("SELECT shipped_at, rejected_at FROM utterances").fetchall()
    assert row["shipped_at"] is None
    assert row["rejected_at"] is not None


async def test_a_400_for_sessions_alone_sets_them_aside() -> None:
    """A session refused on its own would otherwise be refused again every session."""
    conn = seeded_conn([])
    transport = FakeTransport([ship.TransportResponse(status=400)])

    await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url=URL,
        transport=transport,
        sleep=no_sleep,
    )

    assert [len(payload["sessions"]) for _, payload in transport.calls] == [1]
    assert store.unshipped_sessions(conn, "install-1") == []

    again = FakeTransport([])
    await ship.ship(
        conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=again, sleep=no_sleep
    )
    assert again.calls == []


async def test_a_413_is_not_retried() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=413)])
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
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
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=record_sleep,
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
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )
    assert shipped == 1


async def test_retries_give_up_after_a_fixed_number_of_attempts() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=503)] * 10)
    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
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
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=flaky,
        sleep=no_sleep,
    )
    assert shipped == 0
    assert len(attempts) == ship.MAX_ATTEMPTS


async def test_no_endpoint_configured_leaves_everything_unshipped() -> None:
    conn = seeded_conn([utterance()])
    shipped = await ship.ship(
        conn, schema=UTTERANCES, install_id="install-1", url=None, transport=FakeTransport()
    )
    assert shipped == 0
    assert len(store.unshipped_rows(conn, UTTERANCES.rows)) == 1


async def test_debug_mode_never_calls_the_transport_and_marks_nothing_shipped() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    shipped = await ship.ship(
        conn, schema=UTTERANCES, install_id="install-1", url=None, transport=transport, debug=True
    )
    assert shipped == 0
    assert transport.calls == []
    assert len(store.unshipped_rows(conn, UTTERANCES.rows)) == 1


async def test_a_session_travels_with_every_chunk_of_its_rows(monkeypatch) -> None:
    """The first chunk may have been soft-dropped by the server's quota (`202`,
    `accepted: 0`), and a later row whose session the server lacks is refused."""
    monkeypatch.setattr(ship, "CHUNK_ROWS", 1)
    rows = [utterance(seq=1), utterance(seq=2)]
    conn = seeded_conn(rows)
    transport = FakeTransport(
        [
            ship.TransportResponse(status=202, body=b'{"accepted": 0}'),
            ship.TransportResponse(status=202, body=b'{"accepted": 1}'),
        ]
    )
    await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example/v1/ingest",
        transport=transport,
        sleep=no_sleep,
    )

    first_sessions = transport.calls[0][1]["sessions"]
    second_sessions = transport.calls[1][1]["sessions"]
    assert len(first_sessions) == 1
    assert second_sessions == first_sessions
    assert len(transport.calls) == 2  # and not sent a third time on its own


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
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, {**INSTALL, "os": os_name, "arch": arch})
    store.insert_session(conn, UTTERANCES, SESSION)
    store.insert_row(conn, UTTERANCES.rows, utterance())
    transport = FakeTransport()

    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url="https://example.test/v1/ingest",
        transport=transport,
    )

    assert shipped == 0
    assert transport.calls == []
    assert len(store.unshipped_rows(conn, UTTERANCES.rows)) == 1


URL = "https://example/v1/ingest"


async def test_a_400_sets_aside_only_its_own_chunk_and_the_next_one_still_ships(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ship, "CHUNK_ROWS", 1)
    conn = seeded_conn([utterance(seq=1), utterance(seq=2)])
    transport = FakeTransport(
        [ship.TransportResponse(status=400), ship.TransportResponse(status=202, body=b"{}")]
    )

    shipped = await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url=URL,
        transport=transport,
        sleep=no_sleep,
    )

    assert shipped == 1
    states = {
        row["utterance_id"]: (row["shipped_at"] is not None, row["rejected_at"] is not None)
        for row in conn.execute("SELECT * FROM utterances")
    }
    assert states == {"utterance-1": (False, True), "utterance-2": (True, False)}
    # The refused chunk's session was never stored, so the next chunk carries it.
    assert len(transport.calls[1][1]["sessions"]) == 1


async def test_a_429_that_never_clears_stops_the_run_and_sets_nothing_aside() -> None:
    conn = seeded_conn([utterance()])
    transport = FakeTransport([ship.TransportResponse(status=429)] * ship.MAX_ATTEMPTS)
    await ship.ship(
        conn,
        schema=UTTERANCES,
        install_id="install-1",
        url=URL,
        transport=transport,
        sleep=no_sleep,
    )
    (row,) = conn.execute("SELECT shipped_at, rejected_at FROM utterances").fetchall()
    assert (row["shipped_at"], row["rejected_at"]) == (None, None)


async def test_a_session_with_no_utterances_ships_on_its_own() -> None:
    conn = seeded_conn([])
    transport = FakeTransport()

    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)

    ((_, payload),) = transport.calls
    assert payload["utterances"] == []
    assert [s["session_id"] for s in payload["sessions"]] == ["session-1"]
    assert store.unshipped_sessions(conn, "install-1") == []

    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)
    assert len(transport.calls) == 1  # shipped once, not every session


async def test_ending_a_shipped_session_ships_it_again_with_its_end() -> None:
    """A session ships when it starts, before it has an `ended_at`; closing it puts
    it back in the backlog so the next run sends the end."""
    conn = seeded_conn([utterance()])
    transport = FakeTransport()
    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)

    store.end_session(conn, "session-1", "2026-09-13T11:00:00Z")
    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)

    _, payload = transport.calls[-1]
    assert payload["utterances"] == []
    assert payload["sessions"][0]["ended_at"] == "2026-09-13T11:00:00Z"


async def test_a_session_changed_while_its_request_is_in_flight_stays_unshipped() -> None:
    conn = seeded_conn([])

    async def closes_it_meanwhile(url: str, body: bytes) -> ship.TransportResponse:
        store.end_session(conn, "session-1", "2026-09-13T11:00:00Z")
        return ship.TransportResponse(status=202, body=b"{}")

    await ship.ship(
        conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=closes_it_meanwhile
    )

    (pending,) = store.unshipped_sessions(conn, "install-1")
    assert pending["ended_at"] == "2026-09-13T11:00:00Z"


async def test_only_the_current_install_ships_under_its_own_install_row() -> None:
    """Rows left by an earlier install id are neither sent under the current one -
    the server refuses a session whose install is not the request's - nor sent
    with the earlier install's row, whichever of the two sorts first."""
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, {**INSTALL, "install_id": "install-0"})
    store.insert_session(
        conn, UTTERANCES, {**SESSION, "session_id": "session-0", "install_id": "install-0"}
    )
    store.insert_row(
        conn,
        UTTERANCES.rows,
        utterance(utterance_id="old", session_id="session-0", install_id="install-0"),
    )
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, UTTERANCES, SESSION)
    store.insert_row(conn, UTTERANCES.rows, utterance())
    transport = FakeTransport()

    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)

    ((_, payload),) = transport.calls
    assert payload["install"]["install_id"] == "install-1"
    assert {s["install_id"] for s in payload["sessions"]} == {"install-1"}
    assert {u["install_id"] for u in payload["utterances"]} == {"install-1"}
    assert [u["utterance_id"] for u in store.unshipped_rows(conn, UTTERANCES.rows)] == ["old"]


async def test_a_chunk_budgets_for_the_sessions_it_carries() -> None:
    """A backlog of single-utterance sessions - one per `/talk` - puts a session
    beside every row. Counting the rows alone would build a request over the
    server's body cap, which is answered `413` and never delivered."""
    conn = store.connect(Path(":memory:"), UTTERANCES)
    store.upsert_install(conn, INSTALL)
    for n in range(1, 101):
        session_id = f"{n:08d}-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        store.insert_session(
            conn, UTTERANCES, {**SESSION, "session_id": session_id, "harness_version": "9" * 60}
        )
        store.insert_row(
            conn,
            UTTERANCES.rows,
            utterance(seq=n, session_id=session_id, engine="nemo", voice="custom"),
        )
    transport = FakeTransport()

    await ship.ship(conn, schema=UTTERANCES, install_id="install-1", url=URL, transport=transport)

    assert len(transport.calls) > 1
    for _, payload in transport.calls:
        carried = json.dumps(payload["sessions"]) + json.dumps(payload["utterances"])
        assert len(carried) <= ship.CHUNK_BYTES


async def test_debug_mode_logs_a_session_only_batch_and_marks_nothing(caplog) -> None:
    conn = seeded_conn([])
    transport = FakeTransport()
    with caplog.at_level("INFO", logger="speak.telemetry.ship"):
        await ship.ship(
            conn,
            schema=UTTERANCES,
            install_id="install-1",
            url=URL,
            transport=transport,
            debug=True,
        )
    assert transport.calls == []
    assert "telemetry debug, not sent" in caplog.text
    assert len(store.unshipped_sessions(conn, "install-1")) == 1
