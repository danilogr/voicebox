# <img src="../assets/voicebox-detailed.svg" width="28" height="28" alt="" valign="middle" /> Architecture

> System shape and the decisions behind it. Update after an agreed architectural
> change. Keep decisions with their rationale (the "why").

## Overview

voicebox is a coding-agent plugin for offline transcription, voice-overs and reading
aloud. It runs as an MCP server over stdio (`voicebox-mcp`) with six tools —
`transcribe`, `synthesize`, `job_status`, `read_aloud`, `reading_status`,
`stop_reading` — and the commands `/voicebox:narrate`, `/voicebox:setup`,
`/voicebox:telemetry` and `/voicebox:stats`. Everything runs on the machine, on NVIDIA
NeMo-Speech.cpp.

Transcription and synthesis are **jobs**: the tool checks the request, records it, and
starts a detached worker that owns it to the end, so a long job outlives the session
that asked for it. Reading aloud is not a job: sentences go through the speech queue in
the MCP server and play as they are queued.

The engines, the supervisor of the shared speech server, the playback queue, the speaker
lock, the shared settings and the telemetry mechanism are
[speak-core](https://github.com/danilogr/speak-core)'s, a git dependency pinned by tag;
their design is in its `harness/architecture.md`. This document covers voicebox.

## Components & boundaries

- **`__main__`** — the composition root: reads the environment, builds everything, runs
  stdio. Entry points are the only modules that read `os.environ`: `__main__`,
  `synthesize.worker.main` (the server settings it inherited, so an API key is never
  written to disk) and `telemetry.commands`. None installs a log handler except through
  `speak_core.runtime.configure_logging` or the workers' own `basicConfig`.
- **`config.Settings`** — the shared `ServerSettings`, a `VoiceSettings` read under
  `VOICEBOX_` (default engine `nemo`), the transcription settings and the state dir.
- **`server.create_server`** — the transport: tools defined inside a factory that closes
  over its dependencies, no module-level state. It normalises, delegates and formats; it
  holds no job or speech logic. It also serves `commands/narrate.md` as the `narrate`
  prompt.
- **`jobs/`** — what every job kind shares: `store` (one JSON record per job, a subclass
  per kind), `lock` (one job at a time), `desk` (claiming, launching a worker,
  recognising a dead one) and `status` (`job_status`).
- **`transcribe/`** — `service` checks a file and starts `worker`, which converts with
  ffmpeg when needed and runs `nemo-speech transcribe`.
- **`synthesize/`** — `service` checks a request and stages the spoken text; `worker`
  splits it (`text`), synthesizes each chunk over HTTP, joins (`audio`), encodes
  (`encode`) and writes the file. `limits` holds the measured request limits.
- **`narration`** — one telemetry row per reading-aloud run, fed by the queue's
  `on_finish`.
- **`programs`** — running ffprobe and ffmpeg with timeouts.
- **`telemetry/`** — see below.

## Data model

- **Session state** (in memory): whether this session registered with the supervisor,
  whether it has checked the client's `client_info`, the open reading-aloud run.
- **Plugin state** under `VOICEBOX_STATE_DIR` (`<cache>/voicebox`):
  - `job/owner` — the one-job lock: the pid of the session that claimed it, then of the
    worker that took it over.
  - `jobs/<job_id>.json` — every job's record, written by the tool once and by the
    worker after that; `.log` (the worker's stderr), `.pid` (the pid the spawn returned),
    and while a job runs, `.wav` (transcription's converted audio), `.txt` (synthesis's
    staged text) and `.d/` (synthesis's chunk WAVs). Records outlive their job by two
    weeks.
  - `telemetry.db` — the local telemetry store. A session that finds it damaged (SQLite
    says it is not a database, or is malformed) renames it, with its journal, to
    `telemetry.db.corrupt*` and starts an empty one; its unshipped rows are lost. A store
    that is only locked or unreachable is left alone and that session collects nothing.
- **Consent** in `<config>/voicebox/` (`~/Library/Application Support/voicebox`):
  `telemetry.json`, `ping.json`, `notice.json`.
- **Shared state** in `<cache>/speak-core/`: the speech server's lock, pid record and
  client registry, and the speaker lock — speak-core's.

## Telemetry

`telemetry/` is voicebox's part, on `speak_core.telemetry`:

- **`contract.py`** — the `jobs` row table, the schema (`schema.sql`, vendored byte for
  byte from voicebox-telemetry's `contract/v1`), the identity and the store's path.
- **`rows.py`** — `job_row`, the one function every row goes through: it clamps numbers
  to the contract's ceilings, drops strings that do not fit their pattern, records
  unknown voices as `custom`, and keeps `reason` and `stage` only where the contract
  allows them. A caller's bug (a field of another kind, a status the kind cannot have)
  raises instead.
- **`jobs.py`** — a finished job's row, from its record; `record.py` stores it.
- **`session.py`**, **`compose.py`** — one session's row and connection.
- **`notice.py`** — the first-run notice's text and the once-per-session hand-out.
- **`stats.py`**, **`commands.py`** — `voicebox-stats` and `voicebox-telemetry`.

## External dependencies

- **NVIDIA NeMo-Speech.cpp** (https://github.com/NVIDIA/NeMo-Speech.cpp) — the local
  speech stack voicebox exists to wrap: MagpieTTS for speech (reading aloud, voice-overs)
  and Nemotron 3.5 for recognition (transcription, through the `nemo-speech transcribe`
  CLI). Installed by `speak-core-setup` (`/voicebox:setup`); its server is shared by every
  plugin built on speak-core and run by speak-core's supervisor.
- **ffmpeg / ffprobe** — probing and converting media for transcription; m4a and mp3
  voice-overs.
- **`mcp`** — the MCP SDK: stdio transport and tool schemas.
- **`speak-core`** — `speak-core @ git+https://github.com/danilogr/speak-core@<tag>`.
- **`uv`** — environment and launcher; `scripts/voicebox-mcp` finds it.

## Key decisions (with rationale)

- **One job at a time, whatever its kind.** Transcription and synthesis contend for the
  same accelerator, and two at once would each run slower than one after the other. One
  lock (`<state>/job`) covers both; a second call is refused at once with the running
  job's id rather than queued, so the answer is immediate. The lock is not re-entrant
  (the session claims it for a worker that does not exist yet) and waits out an ownerless
  directory for two seconds before reclaiming it, so a session mid-claim is never robbed.
  An in-process gate in the server closes the window between a claim and the worker's
  takeover. Rejected: a queue of jobs, which turns "busy" into a wait nobody asked for.

- **A job is owned by a detached worker, and the record is the source of truth.** A
  three-hour recording is minutes of work and a long voice-over more; nothing should
  depend on the asking session staying open. The tool writes the record once; the worker
  writes every later version; status reads the record and turns a dead worker's job into
  `crashed`, using the pid sidecar the spawn wrote so a worker that died at once is
  noticed at once.

- **Job ids are uuid4s**, because the store's id is telemetry's `job_id`, the contract's
  idempotency key. Whoever ends a job records its row, insert-if-absent: the worker, the
  tool for a refusal, or any process that finds a dead worker (including the sweep every
  session runs at start) — so a crash found twice is one row.

- **A job's telemetry belongs to the session that accepted it.** The record carries that
  session's database path and ids, so a worker that outlives its session still files its
  row there. The setting is read once per session, so a change applies from the next one.

- **synthesize has no fallback.** A voice-over that changes voice halfway is worse than
  an error, so it calls speak-core's `NemoSynthesizer` directly with its own per-request
  timeout and fails with a named reason, where reading aloud falls back to `say`. NeMo is
  required at call time: the server answers, or its binary resolves and voicebox may
  start it.

- **No text is ever dropped, and each request is one sentence.** MagpieTTS repeats and
  garbles words when one request holds several sentences (`performance.md`), so the text
  is sent a sentence at a time, and a sentence over 250 characters is cut at its clauses,
  then its words. A one-word sentence (`Dr.`) goes with the next, and a full stop before
  a lowercase word (`e.g. this`) does not end a sentence. The cutting is speak-core's
  `text.pieces`, which its `nemo` engine applies to `read_aloud` sentences too. A word
  longer than a chunk is cut, never dropped (so text without spaces is cut at the size
  limit). Markdown loses only its markup, list numbers included; fenced code is skipped
  whole, because code read aloud is noise in a voice-over (as `/voicebox:narrate`
  already skips it).

- **A request's timeout follows the machine.** The speech server finishes a request its
  client gave up on, so a retry queues behind work nobody will use. Each request may take
  four times what the slowest pace this job has shown would need, bounded below by the
  Metal figure and above by the CPU backend's pace and 45 minutes; a timeout fails the
  job and is not retried (`performance.md`).

- **A voice-over never overwrites a file.** An explicit output that exists is refused; a
  default one that exists gets the job id in its name. The file is written beside its
  final path and renamed into place, so nobody finds half of it.

- **The worker registers with the supervisor for its whole life.** A session ending
  elsewhere would otherwise stop the shared server mid-job; it starts the server through
  `NemoSupervisor.warm`, which never replaces one already answering.

- **Reading aloud goes through the core queue and the speaker lock.** Sentences from
  every plugin on speak-core interleave rather than overlap, and `stop_reading` ends a
  wait for the speakers at once. `read_aloud` returns at once; `/voicebox:narrate` paces
  itself in rounds checked with `reading_status`, so a "stop" loses seconds.

- **voicebox restarts the queue's worker when it has ended.** speak-core 0.3.0's worker
  ends on an error it does not expect from a player (a `ValueError` from a NUL in the
  player's arguments is one), and nothing reads the queue after that. `read_aloud`
  refuses the text known to cause it; for anything else, the next `read_aloud` or
  `reading_status` finds the worker done, drops what the queue held, starts a new
  worker and says so in its reply.

- **`/voicebox:narrate` is also an MCP prompt.** Claude Code reads `commands/*.md`; every
  other MCP client sees only what the server offers. Serving the same file keeps one copy
  of the instruction.

- **Nothing is collected or sent before the first-run notice has been shown.** It is
  appended once to the first tool reply of a session (with an instruction to show it word
  for word) and printed by `/voicebox:setup`. Not collecting before it also keeps the
  first install count an `install`, which the ping decides from the consent file
  collecting would create. A session that surfaces neither loses its telemetry.

- **A refusal is recorded only for a closed-set reason.** An argument the model only has
  to give differently is answered and not counted; it says nothing about voicebox.

- **A build that is not a release sends nothing.** The contract takes `major.minor.patch`
  versions only; a development build collects locally and logs that it will not send,
  rather than having every request refused.

- **Deliberate tool failures raise `ToolError` or return a sentence.** The SDK hides a raw
  exception's text from the model; a refusal the model can act on travels as text.

- **stdout is the protocol channel.** All logging goes to stderr through one handler;
  the detached workers log to their job log.

- **`.mcp.json` launches `/bin/sh scripts/voicebox-mcp`**, which finds `uv` itself, so a
  parent with a bare `PATH` still starts the server.
