"""Proof that what `ship.py` actually produces from a real local store validates
against the real schema - not just that the fixtures do. `jsonschema` again, for
the same reason as `test_fixtures.py`: a hand-rolled check proves nothing about
what the server's ajv would accept.
"""

import json
from pathlib import Path

import jsonschema

from speak.telemetry import ping, ship, store
from speak.telemetry.contract import IDENTITY, SCHEMA

CONTRACT_DIR = Path(__file__).parent / "v1"
REQUEST_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ingest-request.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(REQUEST_SCHEMA)
PING_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ping-request.schema.json").read_text())

INSTALL = {
    "install_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "first_seen_at": "2026-09-13T10:00:00Z",
    "last_seen_at": "2026-09-13T10:00:00Z",
    "plugin_version": "1.1.0",
    "disclosure_ver": 1,
    "language": "en-US",
    "timezone": "America/Sao_Paulo",
    "utc_offset_min": -180,
    "os": "darwin",
    "os_version": "15.4",
    "arch": "arm64",
    "cpu_model": "Apple M5",
    "cpu_count": 12,
    "ram_gb": 32,
    "accel": "metal",
    "accel_name": "Apple M5",
    "country": None,  # never populated by the client; must still project away
}

SESSION = {
    "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "install_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "started_at": "2026-09-13T10:00:00Z",
    "ended_at": "2026-09-13T10:20:00Z",
    "plugin_version": "1.1.0",
    "tts_engine": "nemo",
    "asr_engine": "nemo",
    "listen_enabled": True,
    "harness": "claude-code",
    "harness_version": "2.1.0",
}


def out_row(**overrides: object) -> dict[str, object]:
    row = {
        "utterance_id": "cccccccc-cccc-4ccc-8ccc-ccccccccccc0",
        "session_id": SESSION["session_id"],
        "install_id": INSTALL["install_id"],
        "seq": 1,
        "direction": "out",
        "source": "speak",
        "occurred_at": "2026-09-13T10:00:01Z",
        "engine": "nemo",
        "model": "nemotron-3.5",
        "voice": "sage",
        "intent": "task_done",
        "words": 42,
        "chars": 231,
        "audio_ms": 8400,
        "queue_wait_ms": 120,
        "synth_ms": 340,
        "play_ms": 8600,
        "status": "ok",
        # local-only, must never survive the projection
        "text": "the actual sentence spoken",
        "text_sha256": "deadbeef",
        "shipped_at": None,
    }
    row.update(overrides)
    return row


def in_row(**overrides: object) -> dict[str, object]:
    row = {
        "utterance_id": "cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
        "session_id": SESSION["session_id"],
        "install_id": INSTALL["install_id"],
        "seq": 2,
        "direction": "in",
        "source": "listen",
        "occurred_at": "2026-09-13T10:00:20Z",
        "engine": "nemo",
        "model": "magpie",
        "device_kind": "bluetooth",
        "words": 9,
        "chars": 47,
        "audio_ms": 2100,
        "asr_ms": 260,
        "status": "ok",
        # local-only, must never survive the projection
        "text": "run the tests",
        "text_sha256": "cafebabe",
        "device_name": "Danilo's AirPods Pro",
        "shipped_at": None,
    }
    row.update(overrides)
    return row


def _payload(*utterances: dict[str, object]) -> dict[str, object]:
    return {
        "install": ship.project_install(INSTALL),
        "sessions": [ship.project_session(SCHEMA, SESSION)],
        "utterances": [ship.project_row(SCHEMA.rows, u) for u in utterances],
    }


def test_a_full_out_and_in_batch_validates() -> None:
    payload = _payload(out_row(), in_row())
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]


def test_the_minimal_out_row_validates() -> None:
    payload = _payload(
        out_row(
            engine=None,
            model=None,
            voice=None,
            intent=None,
            words=None,
            chars=None,
            audio_ms=None,
            queue_wait_ms=None,
            synth_ms=None,
            play_ms=None,
        )
    )
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]


def test_the_real_local_store_round_trips_through_a_validating_payload() -> None:
    """The same path `ship.py` runs in production: insert into the real schema,
    read it back, project it, validate it - not a hand-assembled dict."""
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SCHEMA, SESSION)
    store.insert_row(conn, SCHEMA.rows, out_row())
    store.insert_row(conn, SCHEMA.rows, in_row())

    install = store.install_row(conn, INSTALL["install_id"])
    sessions = store.sessions_by_id(conn, [SESSION["session_id"]])
    utterances = store.unshipped_rows(conn, SCHEMA.rows)
    assert install is not None

    payload = {
        "install": ship.project_install(install),
        "sessions": [ship.project_session(SCHEMA, s) for s in sessions],
        "utterances": [ship.project_row(SCHEMA.rows, u) for u in utterances],
    }
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]


def test_a_transcription_of_an_oversized_file_still_validates() -> None:
    """What the worker builds for a file past the contract's ceilings has to be a row
    the server accepts: it answers anything above one with a 400, and the shipper sets
    that chunk aside, losing every row in it."""
    from speak.telemetry.session import record_transcription
    from speak.transcribe.jobs import DONE, Job
    from speak.transcribe.worker import outcome_for

    job = Job(
        job_id="j1",
        state=DONE,
        source="/private/holiday.mov",
        output="/private/holiday.txt",
        format="text",
        diarize=True,
        language=None,
        translate_to=None,
        duration_s=200_000.0,  # 55 hours
        size_bytes=20 * 1024**3,  # 20 GiB
        codec="aac",
        convert=True,
        asr_model="nemotron-3.5",
        nemo_binary="nemo-speech",
        claimed_by=1,
        created_at=1000.0,
        asr_ms=90_000_000,
        words=600_000,
        chars=6_000_000,
    )
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SCHEMA, SESSION)
    record_transcription(
        conn,
        session_id=SESSION["session_id"],
        install_id=INSTALL["install_id"],
        engine="nemo",
        model="nemotron-3.5",
        outcome=outcome_for(job),
    )

    install = store.install_row(conn, INSTALL["install_id"])
    assert install is not None
    payload = {
        "install": ship.project_install(install),
        "sessions": [
            ship.project_session(SCHEMA, s)
            for s in store.sessions_by_id(conn, [SESSION["session_id"]])
        ],
        "utterances": [
            ship.project_row(SCHEMA.rows, u) for u in store.unshipped_rows(conn, SCHEMA.rows)
        ],
    }
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]


def test_a_transcription_round_trips_through_a_validating_payload() -> None:
    """SQLite hands `converted` and `diarize` back as 0/1 and the contract types
    them `boolean`: the projection is what makes a real row acceptable."""
    from speak.telemetry.session import TranscriptionOutcome, record_transcription

    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SCHEMA, SESSION)
    record_transcription(
        conn,
        session_id=SESSION["session_id"],
        install_id=INSTALL["install_id"],
        engine="nemo",
        model="nemotron-3.5",
        outcome=TranscriptionOutcome(
            status="ok",
            source_bytes=49_283_072,
            audio_codec="flac",
            audio_ms=3_780_000,
            asr_ms=548_000,
            converted=True,
            diarize=True,
            format="json",
            words=9_100,
            chars=51_000,
        ),
    )

    install = store.install_row(conn, INSTALL["install_id"])
    assert install is not None
    payload = {
        "install": ship.project_install(install),
        "sessions": [
            ship.project_session(SCHEMA, s)
            for s in store.sessions_by_id(conn, [SESSION["session_id"]])
        ],
        "utterances": [
            ship.project_row(SCHEMA.rows, u) for u in store.unshipped_rows(conn, SCHEMA.rows)
        ],
    }
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]
    assert payload["utterances"][0]["converted"] is True


async def test_every_request_ship_sends_validates_including_a_session_alone() -> None:
    """The payloads `ship()` itself builds, captured at the transport: an utterance
    chunk, then a session that ships with no utterances at all."""
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SCHEMA, SESSION)
    store.insert_row(conn, SCHEMA.rows, out_row(status="interrupted"))
    lonely = {**SESSION, "session_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}
    store.insert_session(conn, SCHEMA, lonely)
    sent: list[dict[str, object]] = []

    async def transport(url: str, body: bytes) -> ship.TransportResponse:
        sent.append(json.loads(body))
        return ship.TransportResponse(status=202, body=b'{"accepted": 1}')

    await ship.ship(
        conn,
        schema=SCHEMA,
        install_id=str(INSTALL["install_id"]),
        url="https://x/v1/ingest",
        transport=transport,
    )

    assert [len(p["utterances"]) for p in sent] == [1, 0]
    for payload in sent:
        errors = list(VALIDATOR.iter_errors(payload))
        assert errors == [], [e.message for e in errors]


def test_the_install_ping_validates_against_the_contract(tmp_path) -> None:
    pending = ping.due(
        {"HOME": str(tmp_path)},
        IDENTITY,
        plugin_version="1.7.0",
        host_platform="linux",
        ingest_url=IDENTITY.ingest_url({}),
    )
    assert pending is not None
    jsonschema.validate(pending.payload, PING_SCHEMA)
    assert pending.url == "https://speak-telemetry.actionableintelligence.app/v1/ping"
