# Telemetry contract, v1

This is the wire contract between the `speak` client and the telemetry server. Both sides
build against `contract/v1/`, not against each other. See `docs/2026-09-13-telemetry-spec.md`
for the reasoning behind each rule; this document states the rules themselves.

## Endpoints

| Method | Route | Body cap | Notes |
|---|---|---|---|
| `POST` | `/v1/ingest` | 64 KB | batch of install + sessions + utterances; idempotent |
| `GET` | `/v1/health` | — | no data, no auth, returns 200 with an empty body |

### Request shape

`contract/v1/schema/ingest-request.schema.json` is the request body. It is a single object:

```json
{ "install": { ... }, "sessions": [ ... ], "utterances": [ ... ] }
```

- **`install`** (required, one object): the current snapshot of the `installs` row. Sent on
  every request, not just the first — it is small and static, and D1 enforces foreign keys on
  every query (spec 4.8), so the install row must already exist before a session referencing
  it can be inserted. The server upserts by `install_id`; repeating the same values is
  harmless.
- **`sessions`** (required, 0-100 items): the `sessions` rows that the utterances in this
  request belong to. Empty when every session in this chunk was already sent in an earlier
  chunk of the same shipping run (a session with more than 100 utterances spans several
  chunks; only the first chunk needs to carry the session object). The server upserts by
  `session_id`.
- **`utterances`** (required, 1-100 items): the chunk of the local backlog being shipped.
  This is the array the 100-row ceiling and the 48 KB client-side chunk target (spec 4.2,
  11) are sized against. The server inserts, ignoring rows whose `(install_id, utterance_id)`
  already exist (spec 4.4).

`country` is deliberately **not** a property of `install` in the request. It is written by
the server from `CF-IPCountry` on receipt (spec 1.2, 4.5); the client never collects or sends
it, and `additionalProperties: false` rejects a client that tries.

Referential integrity between the three arrays (a session's `install_id` matching the
request's `install.install_id`; an utterance's `session_id` matching a `sessions` entry
present in this request or already known to the server) is **not** schema-checked — that
needs cross-field `$data` references, which standalone ajv does not support (spec 4). It is
enforced by D1's foreign keys and surfaces as `invalid_reference` (below), not as a JSON
Schema fixture.

### Response shape (202)

`contract/v1/schema/ingest-response.schema.json`:

```json
{ "accepted": 100 }
```

`accepted` counts utterance rows now durably stored on the server as a result of this
request — newly inserted or already present from a replayed chunk. It is a batch-level count,
not a per-row list: chunks are acknowledged as a unit (spec 11, "marks each batch shipped as
it is acknowledged"), so the client needs nothing finer-grained to decide what to mark
shipped.

## Status codes

| Code | Meaning | Body |
|---|---|---|
| `202` | batch accepted (idempotent: a full or partial replay also gets `202`) | ingest response |
| `400` | schema violation, or a referential-integrity failure D1 caught | error |
| `404` | unknown route, or an unsupported schema version | error |
| `413` | body over 64 KB | error |
| `429` | rate limited at the edge, by `ip.src`. **Never the per-install quota** — see below | error, `Retry-After` header |
| `500` | anything else; never a stack trace, never route detail (spec 4.5) | error |

## Error shape

`contract/v1/schema/error.schema.json`:

```json
{ "error": { "code": "schema_violation", "message": "...", "field": "/utterances/0/words" } }
```

- `code` is one of `schema_violation`, `invalid_reference`, `payload_too_large`,
  `rate_limited`, `not_found`, `internal`.
- `field`, present whenever the failure names a specific location, is a **JSON Pointer**
  (RFC 6901) into the request body. Construct it from an ajv error as:
  - `additionalProperties` → `error.instancePath + "/" + error.params.additionalProperty`
  - `required` → `error.instancePath + "/" + error.params.missingProperty`
  - every other keyword (`enum`, `pattern`, `minimum`, `maximum`, `maxItems`, the `false`
    schema used by the direction conditionals, ...) → `error.instancePath` unchanged
  - a root-level `additionalProperties` violation has `instancePath === ""`, so `field` is
    just `"/" + additionalProperty` (e.g. `/debug`), not `"/"` followed by more.

  This convention is what `contract/v1/fixtures/invalid/*.expected.json` pins, verified
  against ajv directly (see the repo's commit history for the proof).
- **Pointer granularity is not universal, and the fixtures say where.** JSON Schema does not
  specify how precisely a validator must locate an error, and conforming implementations
  differ: for a `false` schema nested under `properties`, ajv names the property while
  `jsonschema` names the containing object. Both reject; they disagree only on the pointer.

  So an expected file may carry **`"field_is_prefix": true`**, meaning implementations must
  report a pointer that *starts with* `field` rather than equalling it. Where the flag is
  absent — every other fixture — the match is exact. Only `intent-on-in-row` needs it today.

  A contract that demanded identical pointers everywhere would be promising something the
  specification does not provide, and the first honest implementation to disagree would look
  like the one at fault.
- `Retry-After` on `429` is an HTTP header, not a body field — Cloudflare's rate limiter does
  not set it automatically (spec 4.3), so the Worker must.

## The per-install quota does not fail the request

The `install_id` quota **soft-drops**: over the limit, the server still answers `202`, with
`accepted` lower than the number of rows submitted, and stores nothing for the excess. It
never returns `429` (spec 4.3 — a heavy legitimate user should lose telemetry, not have their
plugin start erroring).

So both sides must agree on this, because it is counter-intuitive:

- **`accepted` may be less than the rows sent, and that is not an error.**
- **The client marks the chunk shipped anyway.** The shortfall is a deliberate discard, not a
  failure, and retrying would re-send into the same quota for the rest of the day.
- The client logs the shortfall at debug so it is diagnosable, and does nothing else.

`accepted: 0` is therefore a normal response, distinguishable from a rejection only by the
status code being `202`.

## Retry rules

- The client backs off exponentially with jitter on `429` and any `5xx`, and gives up after a
  small, fixed number of attempts. It must never tight-loop: a retry storm from the clients
  themselves is the likeliest outage (spec 4.3).
- Every retry of the same chunk is safe: `utterance_id` is the idempotency key, insert-or-
  ignore on `(install_id, utterance_id)` (spec 4.4). A chunk that partially applied before a
  failure and is resent in full is not double-counted.
- `400` and `413` are not retried as-is — the payload itself is the problem, not the server's
  availability. (A `413` can recur if resent unchanged; the client's chunking is what should
  have prevented it.)

## Versioning

The schema version is the URL path segment (`/v1/...`). The server accepts the current
version **N** and the immediately preceding version **N-1** concurrently, so a client update
and a server deploy never have to land in the same instant. A request to a version older
than N-1, or to a version that does not exist, gets `404` with `code: "not_found"` — the
generic-errors rule (spec 4.5) applies here too, so this looks the same as any other
not-found route.

There is no `schema_version` field inside the body; the path is the only place the version
lives, matching the one place it is decided (which route the request hit).

## The `-- LOCAL` tag

`contract/v1/schema.sql` is the client's full SQLite schema. A line is local-only, and is
dropped by `contract/v1/derive-remote.py`, iff it carries actual SQL (a column definition, an
index, anything with code before the comment) followed by a trailing comment that is exactly
`-- LOCAL` or starts with `-- LOCAL` followed by a space or a colon.

Any other spelling on such a line — wrong case, no space after `--`, the word "local"
embedded in an ordinary comment on a real definition — is a **malformed tag** and fails the
build (`derive-remote.py` exits non-zero) rather than silently keeping the column. A
whole-line comment that carries no SQL (a section header, a doc comment) is never a tag site
and passes through unexamined even if it discusses the mechanism by name.

The nine local-only columns from spec 1 (`text`, `text_sha256`, `audio_path`,
`audio_sha256`, `device_name`, `corrected_text`, `corrected_at`, `correction_kind`,
`shipped_at`) are tagged, and so is the `utterances_unshipped` index — it is defined on
`shipped_at`, a column that does not exist remotely, so the index cannot survive the
derivation either.

Regenerate after any edit to `schema.sql`:

    python3 contract/v1/derive-remote.py

`schema.remote.sql` is committed; a stale copy is a build-time diff away from being caught; a
contract test regenerating and diffing (spec 8) is the client and server's job, not this
repo's.

## Build note for implementers (ajv standalone)

- **No `$data` references, no macro-style custom keywords** — anywhere in
  `contract/v1/schema/*.json`. Neither is used here; adding one breaks `ajv-cli compile`.
- **`format` is not used in this contract.** Where a regex says the same thing (uuid, semver,
  a loose ISO-8601 timestamp, IANA timezone), `pattern` is used instead, specifically because
  `format` validators are no-ops under standalone compilation unless wired through
  `code.formats` — a silently-passing check is worse than a strict regex.
- **`if`/`then`/`else` and boolean (`true`/`false`) subschemas are used** (the direction-
  dependent field exclusions on `utterance`) and are fully supported by ajv standalone; they
  are core 2020-12 keywords, not a macro extension.
- Each schema file is self-contained — no cross-file `$ref` — so `ajv-cli compile -s
  "contract/v1/schema/*.json"` needs no `-r` flag.
- Verified against `ajv-cli@5` / `ajv@8` in `--spec=draft2020` mode: all three schemas compile
  standalone with no `eval`/`new Function` in the output; every `valid/` fixture validates;
  every `invalid/` fixture is rejected at the instancePath its `.expected.json` names.

## Bounds not given by the spec, and other calls made here

The spec fixes the enums and a few bounds (`ram_gb` bucketing, the 64 KB body cap, the
100-row/48 KB chunk target) exactly; where it does not, the following are this contract's
own numbers, chosen generously above any realistic value and documented here rather than
silently:

| Field | Bound | Reasoning |
|---|---|---|
| `words` | 0-20000 | far above any realistic utterance; rules out corrupted/overflowed counts |
| `chars` | 0-150000 | ~7.5 chars/word headroom over the `words` bound |
| `audio_ms`, `play_ms` | 0-600000 (10 min) | generous ceiling for one utterance's audio |
| `synth_ms`, `asr_ms` | 0-120000 (2 min) | generous ceiling for one synthesis/transcription call |
| `queue_wait_ms` | 0-300000 (5 min) | generous ceiling for a queue delay |
| `seq` | 1-100000 | a session-lifetime counter; not tied to the per-request 100-row cap |
| `disclosure_ver`, `cpu_count` | small integer ceilings (1000, 1024) | sanity bounds against garbage input, not real limits |

Other calls, made because the spec describes the data model and the endpoints but not the
request envelope itself:

- **`install` travels on every ingest request**, not on a separate route — there isn't one
  (spec 4.6 lists exactly `/v1/ingest` and `/v1/health`), and D1's unconditional foreign-key
  enforcement (spec 4.8) means the install row must exist before a session can reference it.
- **`os`, `arch`, and `ram_gb` are enforced as closed sets** (enum), even though the task's
  required list is `direction, source, status, intent, device_kind, accel, harness`. The spec
  itself presents `os`/`arch` as a fixed pipe-separated vocabulary and `ram_gb` as an explicit
  bucket set (spec 1: "bucketed: 8 16 32 64 128"), so treating them as open strings would
  accept garbage the spec has already ruled out.
- **`tts_engine`, `asr_engine`, `engine`, `model`, `voice` are left as free strings**, not
  enums, matching the task's enum list exactly. The spec gives these as examples
  (`say | nemo | command`, `magpie | nemotron-3.5`), not a closed vocabulary, and new engines
  or models should not require a contract version bump.
- **`accel` is required, not merely present-when-known**, even though its SQL column is
  nullable. Spec 1.2 makes `unknown` the mandatory fallback when a probe fails or times out,
  so the application-level contract always has a value; the JSON schema reflects that
  stronger guarantee without changing the SQL column's own nullability.
- **`asr_engine` is a one-value enum (`["nemo"]`)**, matching the SQL comment `nemo | NULL`
  literally: optional in JSON (omit for the SQL-NULL case), and there is currently only one
  legal non-null value.
