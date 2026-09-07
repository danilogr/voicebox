# Architecture

> System shape and the decisions behind it. Update after an agreed
> architectural change. Keep decisions with their rationale (the "why").

## Overview

`speak` is a Claude Code plugin that gives the agent a voice. It runs as an MCP
server over stdio and exposes four tools — `speak`, `stop_speaking`, `list_voices`,
`set_voice` — so the model can say one short sentence out loud at the moments where a
voice beats scrollback. Its single responsibility is turning a sentence into audible
speech without ever blocking the agent and without letting two utterances overlap.

## Components & boundaries

- **`__main__`** — the composition root. Reads the environment, builds every other
  component, configures logging, and starts the transport. It is the only module that
  touches `os.environ` or installs a log handler. It must not contain behaviour worth
  testing on its own.
- **`config.Settings`** — a frozen dataclass built from a mapping. Parses and validates
  the whole environment at that one boundary; unparsable values fall back to defaults
  with a warning rather than raising. It must not read the environment itself, so every
  configuration case is testable without patching globals.
- **`text.normalize`** — collapses whitespace, enforces the length cap at a word
  boundary, rejects empty input. Pure; no I/O.
- **`engines`** — a `Protocol` and three implementations (`say`, `nemo`, `command`).
  An engine turns a sentence into an `Utterance`: a command, its arguments, and an
  optional async cleanup. Engines must not play audio or manage processes themselves.
  In every engine the text is an argv element and is never interpolated into a shell.
- **`queue.SpeechQueue`** — owns playback. Exactly one child process runs at a time.
  `enqueue` is synchronous and returns the number of utterances ahead; a single worker
  task drains the queue. It must not know which engine produced an utterance.
- **`supervisor.NemoSupervisor`** — starts, shares, and stops the local speech server.
  It must not synthesize or play anything.
- **`server.create_server`** — the transport layer. Defines the four tools inside a
  factory that closes over its dependencies, so there is no module-level mutable state
  and tests can build a server over fakes. It must not contain speech logic; it
  normalizes, delegates, and formats the reply.

Components communicate by direct async call. The only indirection is the supervisor's
coordination between separate session processes, which happens through the filesystem.

## Data model

Almost nothing is persistent.

- **Session state** (in memory, per process): the active voice and rate, and whether
  this session has registered with the supervisor. Claude Code runs one server process
  per session, so this is already isolated from every other window. Deliberately never
  written to disk.
- **Speech queue** (in memory, per process): pending utterances and the running child
  process.
- **Shared server state** (on disk, per machine), under `SPEAK_STATE_DIR`:
  - `clients/<pid>` — one empty file per live session. Source of truth for whether the
    speech server is still needed.
  - `server.json` — the running server's pid, port, and start time. Source of truth for
    what to stop.
  - `starting/` — a lock directory. Its existence means a session is mid-start; its
    mtime dates the attempt so an abandoned lock can be taken over.

## External dependencies

- **`mcp`** — the official MCP SDK. Provides the stdio transport, protocol negotiation,
  tool-schema generation from type hints, and the client used by the end-to-end tests.
- **`httpx2`** — async HTTP for the speech server's `/v1/audio/speech` and `/v1/models`
  endpoints and for the health probe. Its `MockTransport` lets the engine be tested
  without a live socket.
- **`uv`** — environment and dependency management, and the launcher. Must be
  resolvable at startup; `scripts/speak-mcp` finds it.
- **macOS `say` and `afplay`** — the default engine and the audio player. The plugin is
  macOS-only in practice.
- **NeMo-Speech.cpp** (optional) — a local neural speech server, installed by
  `scripts/install-nemo.sh` and managed by the supervisor. Not vendored: it is a
  per-platform binary plus about 1.2 GB of models.

## Key decisions (with rationale)

- 2026-09-07 — **The when-to-speak policy lives in the `speak` tool description.**
  A tool description is always in the model's context at the moment the choice is made.
  Rejected: a hook or a skill, neither of which can add a callable tool or is guaranteed
  to be read at that moment.

- 2026-09-07 — **The queue is serial and non-blocking.** `speak` returns immediately
  with a queue position, and exactly one child process runs at a time, so the agent
  never waits on playback and two quick calls never talk over each other. Rejected:
  awaiting playback, which stalls the agent for seconds per sentence.

- 2026-09-07 — **Voice and rate are per connection and never persisted.** One server
  process per session means a plain attribute is already isolated. Rejected: a
  machine-wide voice file, which would leak one window's choice into every other.

- 2026-09-07 — **The speech server is shared across sessions, not per session.** It
  holds roughly a gigabyte of resident memory, so one per window is untenable. Sessions
  are separate processes with no other way to see each other, so they coordinate through
  files: an atomic `mkdir` elects exactly one starter, each session leaves a pid file,
  and the last one out stops the server. Sessions that crash are pruned by pid so a lost
  window cannot pin the memory forever.

- 2026-09-07 — **A cold start reuses the failure path.** When the speech server is
  unreachable the engine falls back to `say` and kicks off a start without awaiting it,
  so the first sentence is spoken immediately rather than after a ten-second warmup, and
  every later sentence uses the neural voice. Rejected: blocking the first utterance on
  the warmup, and starting the server eagerly at session start even when the session
  never speaks.

- 2026-09-07 — **`--device cpu` is always passed.** On Apple silicon the Metal backend
  measured slower than CPU for this workload, so CPU is both simpler and faster here.

- 2026-09-07 — **The `command` engine is a real feature.** It hands the sentence to any
  binary as its final argument, which covers piper, espeak, a script, or a remote
  speaker. It also happens to make the whole system testable without producing audio.

- 2026-09-07 — **Dependencies are injected, and the server is built by a factory.**
  `create_server` closes over its engine, queue, and supervisor; the queue takes its
  process launcher, and the supervisor its spawn, liveness, and kill callables. This is
  what lets the suite cover the real code paths with no audio, no sockets, and no
  speech server. Rejected: module-level singletons, which would force monkeypatching.

- 2026-09-07 — **Deliberate tool failures raise `ToolError`.** The SDK withholds a raw
  exception's text from the model and sends a generic message instead, which would turn
  "unknown voice X; available: …" into unusable feedback. `ToolError` carries the
  message through.

- 2026-09-07 — **A random session voice is resolved lazily, not at startup.** A
  speech server reports no voices until it has warmed up, so rolling at construction
  would always fall back to the default. The roll happens on first use and retries
  until the engine can answer. Per engine, the eligible pool differs: `nemo` uses every
  speaker the model reports, while `say` reports mostly novelty and legacy voices and so
  draws from a curated natural-voice list, narrowed to what is actually installed.
  `SPEAK_VOICE_POOL` overrides both and is taken at its word, so it also works with an
  engine that reports no names at all.

- 2026-09-07 — **`stop_speaking` cancels synthesis already in flight.** A neural
  utterance takes seconds to synthesize, so without a generation counter a sentence
  stopped mid-synthesis would still be spoken once its audio arrived. The queue counts
  an in-flight utterance in its size and discards any whose generation changed.

- 2026-09-07 — **Shutdown silences the queue before tearing down its worker.** The
  worker task owns the running child process, so cancelling it first drops the handle
  and leaves the machine talking after the session is gone. Stopping the queue first
  terminates the child while it is still tracked.

- 2026-09-07 — **stdout is the protocol channel.** All logging goes to stderr, and the
  log handler is installed in one place. A stray `print` in library code desynchronizes
  the session, so library modules use `logging.getLogger(__name__)` only.

- 2026-09-07 — **`.mcp.json` launches `/bin/sh scripts/speak-mcp` rather than `uv`.**
  `/bin/sh` is an absolute path present on every target system, so the launch has no
  PATH dependency; the shim then finds uv via `$SPEAK_UV`, the PATH, and the locations
  uv's installers use. Rejected: invoking `uv` directly, which fails under any parent
  that does not source the user's shell profile.
