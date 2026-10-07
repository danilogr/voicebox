# voicebox telemetry contract, v1

The wire contract between the voicebox client and the voicebox telemetry Worker. Both sides
build against `contract/v1/`, not against each other. The reasoning is in
`docs/2026-10-06-voicebox-telemetry-design.md`; this document states the rules. It is
self-contained: its rules match `speak-telemetry`'s contract by design, but nothing here
depends on that repo.

## Endpoints

| Method | Route | Body cap | Notes |
|---|---|---|---|
| `POST` | `/v1/ingest` | 64 KB | install + sessions + jobs; idempotent |
| `POST` | `/v1/ping` | 1 KB | anonymous install ping; no identifier; `204`, empty body |
| `GET` | `/v1/health` | — | no data, no auth; `200`, empty body |

Production is `https://voicebox-telemetry.actionableintelligence.app`. Every other route,
method or version is `404 not_found`.

## Ingest request

`contract/v1/schema/ingest-request.schema.json`:

```json
{ "install": { ... }, "sessions": [ ... ], "jobs": [ ... ] }
```

- **`install`** (required, one object): the current snapshot of the `installs` row, sent on
  every request. D1 enforces foreign keys on every query, so the install row must exist
  before a session can reference it; the server upserts by `install_id`.
- **`sessions`** (required, 0-100 items): the sessions that are new or changed since they
  were last sent, plus every session a job in this request belongs to, whether or not it was
  sent before: a chunk the quota soft-drops stores nothing, so a job's session must travel
  with it. The server may still accept a job whose session is already stored. The server upserts by `session_id`, so a resent session overwrites the
  stored one; that is how `ended_at` arrives after a session's jobs have shipped. A copy
  without `ended_at` never clears one already stored.
- **`jobs`** (required, 0-100 items): the chunk of the backlog being shipped. The idempotency
  key is `job_id` alone: the server inserts, ignoring a row whose `job_id` is already stored,
  and such a row still counts in `accepted`. **It may be empty: sessions ship
  alone.** A request with neither sessions nor jobs is answered `202 {accepted: 0}` and
  stores nothing, not even the install.
- The client chunks at **100 rows or 48 KB**, whichever comes first, to stay under the
  64 KB cap with room for the envelope.
- `additionalProperties: false` at every level.
- **Every id (`install_id`, `session_id`, `job_id`) is a version 4 uuid**, in either hex case:
  the third group starts with `4` and the fourth with `8`, `9`, `a` or `b`. The client
  generates every id with uuid4; any other version is a `schema_violation`.
- **A NULL column is omitted, never sent as `null`.** No property in the contract accepts
  `null`; the client drops `None`s when it projects a row.
- **`country` is not a property of `install`.** The server writes it from `CF-IPCountry`;
  the client never collects or sends it, and a client that tries is rejected. A request
  without the header leaves the stored country as it was.

### Referential integrity

Checked by the server, not the schema (it needs cross-field references that standalone ajv
does not support). A failure is `400 invalid_reference` with a pointer, and nothing from
the request is stored:

| Condition | `field` |
|---|---|
| a session's `install_id` is not `install.install_id` | `/sessions/N/install_id` |
| a job's `install_id` is not `install.install_id` | `/jobs/N/install_id` |
| a session's `session_id` is already stored for another install | `/sessions/N/session_id` |
| a job's `session_id` is neither in this request nor stored for this install | `/jobs/N/session_id` |

## Job rows

One row per finished job: a transcription, a synthesis or a narration. `kind` selects the
row schema: `job` is a `oneOf` over `transcription_job`, `synthesis_job` and
`narration_job`, each with `additionalProperties: false` and `kind` as a `const`. A field
that belongs to another kind is therefore rejected by construction.

**Common fields** (every kind):

| Field | Type | Required | Notes |
|---|---|---|---|
| `job_id` | uuid | yes | primary key and idempotency key |
| `session_id` | uuid | yes | the session that asked for the job |
| `install_id` | uuid | yes | denormalised |
| `kind` | `transcription \| synthesis \| narration` | yes | |
| `started_at`, `ended_at` | timestamp | yes | |
| `wall_ms` | integer 0-172 800 000 (48 h) | yes | `ended_at - started_at`, measured monotonically |
| `status` | enum, per kind (below) | yes | |
| `reason` | enum | iff `failed` or `refused` | |
| `stage` | enum | iff `failed` or `crashed` | |
| `engine` | string ≤ 32, `^[A-Za-z0-9._-]+$` | yes | the engine that ran, after any fallback; for a `refused` job, the engine that would have run |
| `model` | string ≤ 64, `^[A-Za-z0-9._-]+$` | no | |

**`status`, per kind:**

| Value | Meaning | Kinds |
|---|---|---|
| `ok` | finished and produced its output | all |
| `empty` | the model heard no speech | transcription |
| `stopped` | ended before its last queued sentence played: `stop_reading`, or the session ended | narration |
| `refused` | turned away before any work started | all |
| `failed` | started, then an error ended it | all |
| `crashed` | the detached worker died without recording an outcome | transcription, synthesis |

**`reason`** (closed set; present iff `status` is `failed` or `refused`): `busy`,
`input_missing`, `input_unsupported`, `output_unwritable`, `ffmpeg_missing`, `nemo_missing`,
`server_unavailable`, `timeout`, `conversion_failed`, `model_failed`, `format_mismatch`,
`other`. `other` exists so the client never has to invent a value.

**`stage`** (closed set; present iff `status` is `failed` or `crashed`): `probe`,
`convert`, `asr`, `split`, `synthesize`, `encode`, `write`, `play`. A `crashed` job carries
a stage and no reason.

The two "iff" rules are an `allOf` beside the `oneOf` on `job`, because they hold for every
kind.

**Transcription fields** (`kind: "transcription"` only; all optional):

| Field | Bound |
|---|---|
| `source_bytes` | integer 0-10 737 418 240 (10 GiB) |
| `audio_ms` | integer 0-86 400 000 (24 h): the media's duration |
| `audio_codec` | string ≤ 32, `^[a-z0-9_]+$` (an ffprobe `codec_name`) |
| `has_video` | boolean |
| `converted` | boolean: ffmpeg ran before the model |
| `convert_ms` | integer 0-86 400 000 |
| `asr_ms` | integer 0-86 400 000: model time |
| `diarize` | boolean |
| `speakers` | integer 0-64 |
| `language` | `auto` or a BCP-47 tag, ≤ 35: the requested language, never the detected one |
| `translate` | boolean |
| `format` | `text \| json \| srt \| vtt` |
| `words` | integer 0-500 000 |
| `chars` | integer 0-5 000 000 |

**Synthesis fields** (`kind: "synthesis"` only; all optional):

| Field | Bound |
|---|---|
| `server_warm` | boolean: the speech server was already running at job start |
| `input_kind` | `text \| file` |
| `input_format` | `plain \| markdown` |
| `chars` | integer 0-5 000 000, after markup stripping |
| `words` | integer 0-1 000 000 |
| `paragraphs` | integer 0-100 000 |
| `chunks`, `chunks_done` | integer 0-100 000 each |
| `voice` | string ≤ 64, `^[A-Za-z0-9._-]+$`: a known voice name or `custom` |
| `synth_ms` | integer 0-172 800 000 (48 h): summed model time |
| `encode_ms` | integer 0-86 400 000: concatenation and conversion |
| `audio_ms` | integer 0-172 800 000: the output's duration |
| `format` | `wav \| m4a \| mp3` |
| `output_bytes` | integer 0-10 737 418 240 |

**Narration fields** (`kind: "narration"` only; all optional):

| Field | Bound |
|---|---|
| `server_warm` | boolean |
| `sentences`, `sentences_played` | integer 0-100 000 each |
| `chars` | integer 0-5 000 000 |
| `voice` | string ≤ 64, `^[A-Za-z0-9._-]+$`: a known voice name or `custom` |
| `synth_ms` | integer 0-172 800 000 |
| `audio_ms` | integer 0-172 800 000: audio actually played |
| `fallback_sentences` | integer 0-100 000: spoken by `say` because NeMo was not ready |

A name used by more than one kind (`chars`, `audio_ms`, `format`, `voice`, ...) means the
same thing in each, with each kind's own bound.

**The client clamps every bounded value to its ceiling before storing the row**, so an
oversized value never costs its whole chunk a `400`.

## Ingest response (202)

`contract/v1/schema/ingest-response.schema.json`:

```json
{ "accepted": 100 }
```

`accepted` counts job rows now durably stored as a result of this request: newly inserted,
or already present from a replayed chunk. It is batch-level, because chunks are
acknowledged as a unit. A sessions-only request is answered `accepted: 0`, its normal
answer.

## Status codes

For `POST /v1/ingest` (the ping's are in its own section):

| Code | Meaning | Body |
|---|---|---|
| `202` | batch accepted (a full or partial replay also gets `202`) | ingest response |
| `400` | schema violation, or a referential-integrity failure | error |
| `404` | unknown route, or an unsupported schema version | error |
| `413` | body over 64 KB | error |
| `429` | rate limited at the edge, by client IP. **Never the per-install quota** | error, `Retry-After` header |
| `500` | anything else; never a stack trace, never route detail | error |

## Error shape

`contract/v1/schema/error.schema.json`:

```json
{ "error": { "code": "schema_violation", "message": "...", "field": "/jobs/0/chars" } }
```

- `code` is one of `schema_violation`, `invalid_reference`, `payload_too_large`,
  `rate_limited`, `not_found`, `internal`.
- `field`, present whenever the failure names a location, is a **JSON Pointer** (RFC 6901)
  into the request body, built from a validator error as:
  - `additionalProperties` → `instancePath + "/" + params.additionalProperty`
  - `required` → `instancePath + "/" + params.missingProperty`
  - every other keyword → `instancePath` unchanged
  - a root-level `additionalProperties` violation has `instancePath === ""`, so `field` is
    `"/" + additionalProperty` (e.g. `/debug`).
  - the property name is a reference token, escaped per RFC 6901: `~` becomes `~0`, then `/`
    becomes `~1` (an unknown property `a/b` at the root is `/a~1b`).
  - a pointer longer than 200 characters (the error schema's bound) is cut to 200, dropping
    the first half of a `~0`/`~1` escape or a surrogate pair the cut would split. It then
    names a prefix of the location; only an unknown property name that long produces one.
- Malformed JSON is `400 schema_violation` with no `field`.
- `Retry-After` on `429` is an HTTP header, not a body field. Cloudflare's rate limiter does
  not set it, so the Worker does.

### Pointer precision, and `field_is_prefix`

JSON Schema does not say how precisely a validator must locate an error inside a `oneOf` or
an `if/then/else`, and conforming validators differ. For a job row that matches no kind's
schema, ajv reports one error per kind's schema, the first about whichever kind comes first;
Python's `jsonschema` reports one error at the row itself.

So an `.expected.json` may carry **`"field_is_prefix": true`**: the reported pointer must
start with `field`, segment by segment, rather than equal it. Every fixture whose violation
sits inside a job row carries it with `"field": "/jobs/0"`. Without the flag the match is
exact.

The Worker reports the exact field for a job row: it re-checks a failing row against its
own kind's schema and names that error (`/jobs/0/voice`, not just `/jobs/0`). A client may
rely only on the prefix.

## The per-install quota does not fail the request

Each install may store **100 rows per UTC day, counting jobs plus sessions**. The quota
**soft-drops**: over it, the server still answers `202`, with `accepted` lower than the jobs
submitted (in practice `0`), and stores nothing from that request. It never returns `429`: a
heavy legitimate user should lose telemetry, not have their plugin start erroring.

- **`accepted` may be less than the jobs sent, and that is not an error.**
- **The client marks the chunk shipped anyway.** Retrying would re-send into the same quota.
- The client logs the shortfall at debug and does nothing else.

The check runs before a request's rows are added, so the request that crosses 100 still
lands and the next one is dropped. A sessions-only request is metered like any other. Only
a stored request counts: one answered `400` or `500` adds nothing to the day's count, so a
retry after a `500` is not soft-dropped on its account.

## Retry rules

- The client backs off exponentially with jitter on `429` and any `5xx`, and gives up after
  a small, fixed number of attempts. It never tight-loops.
- Every retry of the same chunk is safe: `job_id` is the idempotency key. A chunk that
  partially applied before a failure and is resent in full is not double-counted.
- `400` and `413` are not retried: the payload is the problem. The client records the
  chunk's rows as rejected and never resends them.

## The install ping

`POST /v1/ping`, body `contract/v1/schema/ping-request.schema.json`:

```json
{ "plugin_version": "1.0.0", "os": "darwin", "event": "install" }
```

- **`plugin_version`** (required): a release version, `major.minor.patch` with bounded
  digits and no pre-release or build suffix. A suffix is free text that could carry an
  identifier, and every distinct value is a new counter row.
- **`os`** (required): `darwin | linux | win32`, the same set as `install.os`.
- **`event`** (required): `install` the first time voicebox runs on a machine, `upgrade`
  the first time a new version runs.

No other property is accepted. There is no `install_id` and no telemetry mode: the client
sends this same ping once per install and once per upgrade whether telemetry is `on`,
`local` or `off`, and sends nothing at all under `DO_NOT_TRACK=1`. Because the body is
identical in every mode, a ping never announces that someone opted out, and because it
carries no identifier, two pings cannot be linked to each other or to an install.

The server keeps one counter per UTC day, `plugin_version`, `os`, `event` and country
(`CF-IPCountry`, or `''` when absent) and adds one to it. It stores no row per request, no
IP and no time finer than the day.

| Code | Meaning | Body |
|---|---|---|
| `204` | ping counted | empty |
| `400` | not JSON, or a schema violation; `field` as for ingest | error |
| `404` | any method but `POST`, or any other version | error |
| `413` | body over 1 KB | error |
| `429` | rate limited at the edge, by client IP, as every route | error, `Retry-After` header |
| `500` | anything else | error |

There is no per-install quota on the ping. The client sends it at most once per version,
retries a failed one on its next session rather than in a loop, and treats any `2xx` as
done. Fixtures are in `contract/v1/fixtures/ping/{valid,invalid}`.

## Versioning

The schema version is the URL path segment (`/v1/...`). The server accepts the current
version **N** and the one before it, **N-1**, so a client update and a server deploy never
have to land at the same instant. Any other version is `404 not_found`, indistinguishable
from any other unknown route. There is no version field in the body.

**Additive changes land in `v1` in place**: a new `reason` or `stage` value, a new optional
field. The server is deployed before any client sends the new value; until then the client
maps an unknown reason to `other`. A change that would reject a payload valid today needs
`/v2/`.

## The `-- LOCAL` tag

`contract/v1/schema.sql` is the client's full SQLite schema. A line is local-only, and is
dropped by `contract/v1/derive-remote.py`, iff it carries SQL followed by a trailing comment
that is exactly `-- LOCAL` or starts with `-- LOCAL` followed by a space or a colon.

Any other spelling on a SQL line that mentions "local" (wrong case, no space after `--`, the
word inside an ordinary comment) is a **malformed tag** and fails the build rather than
silently keeping the column. A whole-line comment carries no SQL and is never a tag site.

The local-only columns: on `sessions`, `shipped_at`; on `jobs`, `error_message` (may contain
paths), `source_name` (the input's basename, for the user's own stats), `output_path`,
`shipped_at` and `rejected_at`. So are the `sessions_unshipped` and `jobs_unshipped`
indexes, which are defined on a column that does not exist remotely. None is a property in
the JSON schema, so an upload carrying one is a `schema_violation` naming it.

Regenerate after any edit to `schema.sql`:

    python3 contract/v1/derive-remote.py

`schema.remote.sql` is committed. The server's test suite regenerates it and fails on any
difference.

## Build note for implementers (ajv standalone)

- **No `$data`, no macro keywords, no `format`** anywhere in `contract/v1/schema/*.json`.
  Where a regex says the same thing (uuid, semver, timestamp, IANA timezone, BCP-47), a
  `pattern` is used, because standalone `format` checks are no-ops unless wired through
  `code.formats`.
- `oneOf`, `if/then/else`, `const` and boolean subschemas are used; all are core 2020-12 and
  compile standalone.
- Each status rule's `then` reads `{"required": ["reason"], "properties": {"reason": true}}`.
  The `properties` entry changes nothing a validator accepts; it is there because ajv's
  strict mode (`strictRequired`) refuses a `required` name not defined beside it.
- Each schema file is self-contained, with no cross-file `$ref`.
- **Every `pattern` has JavaScript regex semantics**, because the Worker validates with ajv
  (`new RegExp(pattern, "u")`). In JavaScript, `$` matches only at the end of the string; in
  Python's `re`, which `jsonschema` uses through `re.search`, `$` also matches just before a
  trailing newline. So `"1.0.0\n"` passes Python `jsonschema` and is a `400` here. A Python
  client strips every value bound for a `pattern` field and checks it with `re.fullmatch`
  before sending it, and a value that fails is a bug to fix locally, not a row to ship.
- **Timestamps are sent in UTC, with the `Z` suffix.** The timestamp pattern also accepts a
  `±hh:mm` offset, and the server converts one when it places a job on its UTC day, but only
  UTC timestamps sort in time order as text, so a client sends UTC only.
- The schemas are built to compile under `ajv@8` with `--spec=draft2020 --strict=true` and
  to produce standalone validators with no `eval` or `new Function`. The same fixtures must
  pass under Python `jsonschema`'s `Draft202012Validator`.

### Fixtures

`contract/v1/fixtures/` holds 16 `valid/` bodies and 85 `invalid/` bodies: 35 `ceiling-*` (one
job field just past its ceiling), 9 `install-ceiling-*` and 4 `session-ceiling-*` (one install or
session field past its ceiling); `fixtures/ping/` holds 2 `valid/` and 8 `invalid/`.
Every `invalid` body has an `.expected.json` with the pointer in `field` (and
`field_is_prefix` where it applies). Every `valid/` body validates; every `invalid/` body is
rejected at its expected pointer. The two oversized bodies (`jobs-over-100-rows`,
`sessions-over-100-rows`) exist for the 100-item limits.

## Bounds and other calls this contract makes

The spec fixes the enums, the per-kind field bounds, the 64 KB cap and the 100-row chunk.
Where it leaves a detail open, these are this contract's own calls:

| Call | Reason |
|---|---|
| `wall_ms` 0-172 800 000 (48 h) on every kind | the longest job bound in the spec is a 48 h synthesis |
| `status` is a per-kind set | `empty` and `stopped` are defined for one kind each; narration runs in the MCP server, has no worker, so cannot be `crashed` |
| `reason` and `stage` are one shared set on every kind | the spec gives one closed set each; `other` covers the rest |
| `stage` is required, not just allowed, on `failed` and `crashed` | "iff" in the spec; a failure without a location answers nothing |
| `engine` is required; `model` is optional | the spec types `engine` as non-null and `model` as nullable |
| `server_warm` is a synthesis and narration field only | transcription loads its model per job and never uses the server |
| every kind-specific field is optional | a refused job has no audio, no output and no counts |
| `engine`, `model`, `voice` are bounded strings matching `^[A-Za-z0-9._-]+$`, not enums | new engines, models and voices must not need a contract change; the pattern keeps paths and free text out, and tightening it later would need `/v2/`. A voice name with a space or other character is rejected, so the client maps such voices to `custom` |
| `install` bounds (`disclosure_ver` ≤ 1000, `cpu_count` 1-1024, `utc_offset_min` -720..840, `ram_gb` bucket set) | sanity bounds against garbage |
| ids are version 4 uuids only | the client generates ids with uuid4; admitting other versions now would leave a tightening that needs `/v2/` |
| `core_version` and `nemo_version` use the semver pattern | `nemo-speech --version`'s second field is a plain version (e.g. `0.2.0`) |
| `install` travels on every ingest request | D1's foreign keys need the install row before a session references it |
| `accel` is required although its column is nullable | the client always has a value: `unknown` when the probe fails |
