"""What this client stores and ships validates against the vendored contract: every
valid fixture round-trips through the real local store and projection unchanged, every
row `job_row` builds validates once stored and projected, and a row whose session does
not exist is refused locally, as D1 refuses it."""

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from operator import itemgetter
from pathlib import Path

import jsonschema
import pytest
from speak_core.telemetry import ping, ship, store

from voicebox.telemetry.contract import IDENTITY, JOBS, SCHEMA
from voicebox.telemetry.rows import (
    NARRATION,
    STATUSES,
    SYNTHESIS,
    TRANSCRIPTION,
    JobFacts,
    iso,
    job_row,
    recorded_voice,
)

CONTRACT_DIR = Path(__file__).parent / "v1"


def _javascript_pattern(
    validator: object, pattern: str, instance: object, schema: object
) -> Iterator[jsonschema.ValidationError]:
    """`pattern` as the server's ajv reads it: every contract pattern is `^...$`, and
    JavaScript's `$` does not match before a trailing newline, as Python's does."""
    if isinstance(instance, str) and not re.search(pattern.removesuffix("$") + r"\Z", instance):
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


STRICT = jsonschema.validators.extend(
    jsonschema.Draft202012Validator, {"pattern": _javascript_pattern}
)
REQUEST = STRICT(json.loads((CONTRACT_DIR / "schema" / "ingest-request.schema.json").read_text()))
PING_SCHEMA = json.loads((CONTRACT_DIR / "schema" / "ping-request.schema.json").read_text())
VALID = sorted((CONTRACT_DIR / "fixtures" / "valid").glob("*.json"))

INSTALL = {
    "install_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "first_seen_at": "2026-10-06T10:00:00Z",
    "last_seen_at": "2026-10-06T10:00:00Z",
    "plugin_version": "1.0.0",
    "disclosure_ver": 1,
    "os": "darwin",
    "arch": "arm64",
    "accel": "metal",
    "country": None,  # the client never sets it; it must still project away
}
SESSION = {
    "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "install_id": INSTALL["install_id"],
    "started_at": "2026-10-06T10:00:00Z",
    "plugin_version": "1.0.0",
    "core_version": "0.3.0",
    "nemo_version": None,
    "harness": "claude-code",
    "ffmpeg_present": True,
}

# Every field of every kind, each past its ceiling or outside its pattern where it has one.
OUT_OF_CONTRACT = {
    TRANSCRIPTION: {
        "source_bytes": 20 * 1024**3,
        "audio_ms": 200_000_000,
        "audio_codec": "mp4a/40",
        "has_video": True,
        "converted": True,
        "convert_ms": 10**10,
        "asr_ms": 10**10,
        "diarize": True,
        "speakers": 500,
        "language": "português",
        "translate": False,
        "format": "text",
        "words": 10**7,
        "chars": 10**8,
    },
    SYNTHESIS: {
        "server_warm": False,
        "input_kind": "file",
        "input_format": "markdown",
        "chars": 10**8,
        "words": 10**8,
        "paragraphs": 10**6,
        "chunks": 10**6,
        "chunks_done": 10**6,
        "voice": recorded_voice("nemo", "My Voice"),
        "synth_ms": 10**10,
        "encode_ms": 10**10,
        "audio_ms": 10**10,
        "format": "mp3",
        "output_bytes": 10**12,
    },
    NARRATION: {
        "server_warm": True,
        "sentences": 10**6,
        "sentences_played": 10**6,
        "chars": 10**8,
        "voice": recorded_voice("say", "Samantha"),
        "synth_ms": 10**10,
        "audio_ms": 10**10,
        "fallback_sentences": 10**6,
    },
}


def opened() -> sqlite3.Connection:
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, {k: v for k, v in INSTALL.items() if v is not None})
    store.insert_session(conn, SCHEMA, SESSION)
    return conn


def payload(conn: sqlite3.Connection) -> dict[str, object]:
    install = store.install_row(conn, INSTALL["install_id"])
    assert install is not None
    return {
        "install": ship.project_install(install),
        "sessions": [
            ship.project_session(SCHEMA, s)
            for s in store.sessions_by_id(conn, [SESSION["session_id"]])
        ],
        "jobs": [ship.project_row(JOBS, r) for r in store.unshipped_rows(conn, JOBS)],
    }


def facts(kind: str, status: str, **overrides: object) -> JobFacts:
    fields = dict(
        job_id=str(uuid.uuid4()),
        session_id=SESSION["session_id"],
        install_id=INSTALL["install_id"],
        kind=kind,
        started_at=iso(1_790_000_000),
        ended_at=iso(1_790_000_042.5),
        wall_ms=42_500,
        status=status,
        engine="nemo",
        model="magpie",
        reason="timeout",
        stage="asr",
        error_message="/Users/someone/private/meeting.m4a: boom",
        source_name="meeting.m4a",
        output_path="/Users/someone/private/meeting.txt",
    )
    return JobFacts(**{**fields, **overrides})


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_every_valid_fixture_round_trips_through_the_store_unchanged(path: Path) -> None:
    body = json.loads(path.read_text())
    conn = store.connect(Path(":memory:"), SCHEMA)
    store.upsert_install(conn, body["install"])
    for session in body["sessions"]:
        store.insert_session(conn, SCHEMA, session)
    for job in body["jobs"]:
        store.insert_row(conn, JOBS, job)

    install = store.install_row(conn, body["install"]["install_id"])
    assert install is not None
    sessions = store.sessions_by_id(conn, [s["session_id"] for s in body["sessions"]])
    jobs = store.unshipped_rows(conn, JOBS)

    by_session, by_job = itemgetter("session_id"), itemgetter("job_id")
    assert ship.project_install(install) == body["install"]
    assert sorted((ship.project_session(SCHEMA, s) for s in sessions), key=by_session) == (
        sorted(body["sessions"], key=by_session)
    )
    assert sorted((ship.project_row(JOBS, j) for j in jobs), key=by_job) == sorted(
        body["jobs"], key=by_job
    )


@pytest.mark.parametrize(
    ("kind", "status"), [(k, s) for k, statuses in STATUSES.items() for s in sorted(statuses)]
)
def test_every_status_of_every_kind_validates_minimal_and_maximal(kind: str, status: str) -> None:
    conn = opened()
    store.insert_row(conn, JOBS, job_row(facts(kind, status)))
    store.insert_row(conn, JOBS, job_row(facts(kind, status), OUT_OF_CONTRACT[kind]))
    errors = list(REQUEST.iter_errors(payload(conn)))
    assert errors == [], [e.message for e in errors]


def test_out_of_contract_values_still_produce_a_valid_row() -> None:
    """Review Focus 4: a 20 GiB video, a 60-hour job, a voice named "My Voice", a model
    given as a path, a codec with a slash - the row is still one the server accepts."""
    conn = opened()
    for kind in (TRANSCRIPTION, SYNTHESIS, NARRATION):
        row = job_row(
            facts(kind, "ok", wall_ms=60 * 3_600_000, model="/models/x.gguf", engine="my engine"),
            OUT_OF_CONTRACT[kind],
        )
        store.insert_row(conn, JOBS, row)
    sent = payload(conn)
    errors = list(REQUEST.iter_errors(sent))
    assert errors == [], [e.message for e in errors]
    for job in sent["jobs"]:  # type: ignore[union-attr]
        assert "model" not in job and job["engine"] == "unknown"
        assert not {"error_message", "source_name", "output_path"} & set(job)


def test_a_row_whose_session_does_not_exist_is_refused_locally() -> None:
    conn = opened()
    orphan = job_row(facts(TRANSCRIPTION, "ok", session_id=str(uuid.uuid4())))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_row(conn, JOBS, orphan)


async def test_every_request_ship_sends_validates_including_a_session_alone() -> None:
    conn = opened()
    store.insert_row(conn, JOBS, job_row(facts(SYNTHESIS, "ok"), OUT_OF_CONTRACT[SYNTHESIS]))
    lonely = {**SESSION, "session_id": str(uuid.uuid4())}
    store.insert_session(conn, SCHEMA, lonely)
    sent: list[dict[str, object]] = []

    async def transport(url: str, body: bytes) -> ship.TransportResponse:
        sent.append(json.loads(body))
        return ship.TransportResponse(status=202, body=b'{"accepted": 1}')

    await ship.ship(
        conn,
        schema=SCHEMA,
        install_id=INSTALL["install_id"],
        url="https://x/v1/ingest",
        transport=transport,
    )
    assert [len(p["jobs"]) for p in sent] == [1, 0]  # type: ignore[arg-type]
    for body in sent:
        assert list(REQUEST.iter_errors(body)) == []


def test_the_install_ping_validates_and_goes_to_voiceboxs_origin(tmp_path) -> None:
    pending = ping.due(
        {"HOME": str(tmp_path)},
        IDENTITY,
        plugin_version="1.0.0",
        host_platform="darwin",
        ingest_url=IDENTITY.ingest_url({}),
    )
    assert pending is not None
    jsonschema.validate(pending.payload, PING_SCHEMA)
    assert pending.url == "https://voicebox-telemetry.actionableintelligence.app/v1/ping"


def test_every_id_the_client_mints_is_a_uuid4_the_contract_accepts(tmp_path) -> None:
    """The contract takes version-4 uuids only: the install id core mints, the session
    id and the job id voicebox mints."""
    import re

    from speak_core.telemetry import consent

    from voicebox.jobs.store import new_job_id

    pattern = REQUEST.schema["$defs"]["uuid"]["pattern"]
    install_id = consent.load_consent(
        {"HOME": str(tmp_path)}, IDENTITY, host_platform="darwin"
    ).install_id
    for minted in (install_id, new_job_id(), str(uuid.uuid4())):
        assert re.fullmatch(pattern, minted), minted


def test_the_strict_validator_rejects_what_plain_jsonschema_lets_through() -> None:
    engine = {"type": "string", "pattern": "^[A-Za-z0-9._-]+$"}
    assert jsonschema.Draft202012Validator(engine).is_valid("nemo\n")
    assert not STRICT(engine).is_valid("nemo\n")
    assert STRICT(engine).is_valid("nemo")


def test_adversarial_values_go_through_job_row_to_a_valid_row() -> None:
    conn = opened()
    adversarial = [
        (TRANSCRIPTION, "ok", {"language": "en\n", "audio_codec": "aac\n", "format": "docx"}),
        (SYNTHESIS, "ok", {"voice": "John\n", "input_kind": "url", "input_format": "html"}),
        (NARRATION, "ok", {"voice": "MyPrivateClone"}),
        (SYNTHESIS, "ok", {"format": "docx"}),
    ]
    for kind, status, fields in adversarial:
        store.insert_row(conn, JOBS, job_row(facts(kind, status, model="a" * 65), fields))
    store.insert_row(
        conn, JOBS, job_row(facts(TRANSCRIPTION, "failed", model="a" * 65, reason="cosmic_rays"))
    )
    sent = payload(conn)
    errors = list(REQUEST.iter_errors(sent))
    assert errors == [], [e.message for e in errors]
    jobs = sent["jobs"]
    assert all("model" not in j for j in jobs)  # type: ignore[union-attr]
    assert [j["reason"] for j in jobs if j["status"] == "failed"] == ["other"]  # type: ignore[union-attr]
