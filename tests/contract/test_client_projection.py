"""Proof that what `ship.py` actually produces from a real local store validates
against the real schema - not just that the fixtures do. `jsonschema` again, for
the same reason as `test_fixtures.py`: a hand-rolled check proves nothing about
what the server's ajv would accept.
"""

import json
from pathlib import Path

import jsonschema

from speak.telemetry import ship, store

CONTRACT_DIR = Path(__file__).parent / "v1"
REQUEST_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ingest-request.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(REQUEST_SCHEMA)

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
        "install": ship._project_install(INSTALL),
        "sessions": [ship._project_session(SESSION)],
        "utterances": [ship._project_utterance(u) for u in utterances],
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
    conn = store.connect(Path(":memory:"))
    store.upsert_install(conn, INSTALL)
    store.insert_session(conn, SESSION)
    store.insert_utterance(conn, out_row())
    store.insert_utterance(conn, in_row())

    install = store.install_row(conn, INSTALL["install_id"])
    sessions = store.sessions_by_id(conn, [SESSION["session_id"]])
    utterances = store.unshipped_utterances(conn)
    assert install is not None

    payload = {
        "install": ship._project_install(install),
        "sessions": [ship._project_session(s) for s in sessions],
        "utterances": [ship._project_utterance(u) for u in utterances],
    }
    errors = list(VALIDATOR.iter_errors(payload))
    assert errors == [], [e.message for e in errors]
