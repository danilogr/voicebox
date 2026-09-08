# Architecture

> System shape and the decisions behind it. Update after an agreed
> architectural change. Keep decisions with their rationale (the "why").

## Overview

`speak` is a Claude Code plugin that gives the agent a voice. It runs as an MCP
server over stdio and exposes four tools — `speak`, `stop_speaking`, `list_voices`,
`set_voice` — so the model can say one short sentence out loud at the moments where a
voice beats scrollback. Its single responsibility is turning a sentence into audible
speech without ever blocking the agent and without letting two utterances overlap.

With `SPEAK_LISTEN=1` it also hears. A fifth tool, `listen`, records the user and
returns a transcript, and the `/talk` slash command dictates a prompt instead of
typing it. There are therefore two entry points into the same code: the long-lived
MCP server (`__main__`) and a short-lived one-shot process (`talk`) that a slash
command runs and whose stdout is injected into the user's message. Both transcribe
against the same shared speech server the supervisor already manages. With
`SPEAK_LISTEN` unset the `listen` tool is never defined, so the tool list is
unchanged; `/talk` is a static command file that still appears in the palette, and
the script behind it refuses at once with one line on stderr.

## Components & boundaries

- **`__main__`** — the composition root for the MCP server. Reads the environment,
  builds every other component, and starts the transport. It must not contain
  behaviour worth testing on its own. Voice input added a second entry point, so the
  boundary rule now names both: `__main__` and `talk` are the only modules that read
  this plugin's configuration from `os.environ`, and neither installs a log handler
  itself — both call `runtime.configure_logging`. One other module touches the
  environment at all: `engines.say` reads `LANG` to filter `say -v '?'` by locale,
  which is the platform's setting rather than the plugin's configuration.
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
  task drains the queue. `drain()` waits until nothing is queued, being synthesized, or
  playing, and is part of the `Queue` protocol because `listen` opens the microphone
  only once the speaker has finished. It must not know which engine produced an
  utterance.
- **`supervisor.NemoSupervisor`** — starts, shares, and stops the local speech server.
  It must not synthesize or play anything.
- **`server.create_server`** — the transport layer. Defines the tools inside a
  factory that closes over its dependencies, so there is no module-level mutable state
  and tests can build a server over fakes. It must not contain speech logic; it
  normalizes, delegates, and formats the reply. `listen` is defined only when the
  factory is given an `Ears`, which bundles the recorder, the transcriber, the
  microphone lock and the capture limits into one presence check.
- **`capture`** — the microphone. `SilenceTracker` is a pure state machine fed block
  energies and decides when an utterance has ended; `MicrophoneRecorder` is a thin
  shell that opens a `sounddevice` stream and feeds it. The split is what lets every
  capture path be tested without audio hardware.
- **`engines.asr`** — transcription over the speech server's OpenAI-compatible
  route. It must not record anything.
- **`miclock`** — one holder at a time for the single microphone, elected the same
  way the speech server's start lock is. It must not know what the microphone is for.
- **`runtime`** — logging and the health probe, shared by the two entry points. It
  installs the one log handler, on stderr, and is the only module that installs one.
- **`talk`** — the `/talk` one-shot: hold the shared server for the life of the
  process, record, transcribe, print. It is the only module besides `__main__` that
  reads the plugin's configuration from the environment, and the only one where
  printing to stdout is the point.

Components communicate by direct async call. The only indirection is the supervisor's
coordination between separate session processes, which happens through the filesystem —
and the microphone lock, which coordinates the same way for the same reason.

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
  - `mic/owner` — the pid of the session currently recording. Its directory is the
    lock; the file says who holds it, so a lock left by a dead session is reclaimed.

Nothing about voice input is persistent either. A recording lives in memory as WAV
bytes for the length of one transcription and is never written to disk.

## External dependencies

- **`mcp`** — the official MCP SDK. Provides the stdio transport, protocol negotiation,
  tool-schema generation from type hints, and the client used by the end-to-end tests.
- **`httpx2`** — async HTTP for the speech server's `/v1/audio/speech`,
  `/v1/audio/transcriptions` and `/v1/models` endpoints and for the health probe. Its
  `MockTransport` lets the engine and the transcriber be tested without a live socket.
- **`sounddevice`** — the microphone binding, and the only native-wheel dependency
  here. Imported inside the recorder rather than at module scope, so a machine without
  a working PortAudio still runs the speech half of the plugin.
- **`uv`** — environment and dependency management, and the launcher. Must be
  resolvable at startup; `scripts/speak-mcp` finds it.
- **macOS `say` and `afplay`** — the default engine and the audio player. The plugin is
  macOS-only in practice.
- **NeMo-Speech.cpp** (optional) — a local neural speech server, installed by
  `scripts/install-nemo.sh` and managed by the supervisor. Not vendored: it is a
  per-platform binary plus 1.2 GB of models — MagpieTTS and its codec at 520 MB,
  nemotron-3.5 at 710 MB, measured on 2026-09-07 from the model cache under
  `~/Library/Caches/NeMoSpeech`, and the ASR figure confirmed by pulling into an empty
  `NEMO_SPEECH_MODEL_DIR`.

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

  `/talk` is the exception that proves it: its stdout is a prompt, not a protocol,
  and a failure there is printed to stdout as well as stderr. `commands/talk.md`
  branches on a line beginning `speak:`, and only stdout is certainly injected —
  whether a slash command's `!` capture carries stderr from a non-zero exit is not
  this plugin's to guarantee, and if it does not, stderr alone leaves the model an
  empty transcript under an instruction to act on it as the user's request. The
  prefix is what keeps the stdout copy from being read as speech.

- 2026-09-07 — **`.mcp.json` launches `/bin/sh scripts/speak-mcp` rather than `uv`.**
  `/bin/sh` is an absolute path present on every target system, so the launch has no
  PATH dependency; the shim then finds uv via `$SPEAK_UV`, the PATH, and the locations
  uv's installers use. Rejected: invoking `uv` directly, which fails under any parent
  that does not source the user's shell profile.

- 2026-09-07 — **`listen` drains the speech queue before opening the microphone.**
  `speak` returns immediately by design, so a question is usually still playing when
  the next tool call runs, and without the drain the recording captures the agent's
  own voice. `SpeechQueue.drain()` exists for this and waits out synthesis as well as
  playback. A drain that does not finish inside a minute is logged and the microphone
  is opened anyway: refusing the user is worse than the risk of hearing ourselves.
  Rejected: a combined `ask(question)` tool, which duplicates `speak` to buy only the
  ordering the drain already gives.

- 2026-09-07 — **Recording stops on silence, not on a keypress.** Neither entry point
  has a terminal: the MCP server owns stdin for the protocol, and a slash command's
  bash output is captured. There is no key to press, so silence is the only available
  end-of-turn signal. The gate is calibrated from the room's own ambient level over
  the first 300 ms rather than fixed, because a fixed threshold fails on a quiet
  microphone and in a noisy room alike.

- 2026-09-07 — **ASR is served by the shared speech server, not a one-shot
  subprocess.** The model stays resident, so a transcription costs a request rather
  than a model load. The supervisor already elects one server per machine and tears it
  down when the last session leaves, so this is the existing lifecycle carrying a
  second model. Rejected: `nemo-speech transcribe` per capture, which pays the load
  every time.

- 2026-09-07 — **`--asr-model` is only added when listening is enabled.** A missing
  ASR model makes `serve` fail, which would take neural speech down with it. Gating
  the flag means a session that never listens cannot regress, and a session that opts
  in without running setup degrades to the `say` fallback that already exists rather
  than breaking.

  The cost of gating is a mixed machine: a session using neural speech without
  listening can win the start election and leave an ASR-less server running, which a
  listening session then finds. `listen` cannot tell that apart from an unpulled model
  by probing alone, so it names both causes rather than guessing one. It does not
  restart the server to fix itself — that would cut off whichever session is
  mid-sentence.

- 2026-09-07 — **The microphone is arbitrated by a lock, and contention fails fast.**
  There is one microphone and any number of sessions; two recordings at once produce
  garbage in both. The election is the supervisor's, reused: an atomic `mkdir` on
  `mic/`, an owner pid in the file inside it, and a lock left by a dead session
  reclaimed by the next caller. Rejected: queueing on the lock, which is worse than
  refusing — you would start talking and a window you are not looking at would take
  the words.

- 2026-09-07 — **A second, in-process lock guards `listen` against itself.** The
  microphone lock is re-entrant by pid, which is what makes reclaiming a dead owner's
  lock possible but means it does not separate two calls inside one session: both
  would acquire it, and the first to finish would release the cross-process lock while
  the other was still recording. `listen` therefore also holds an `asyncio.Lock` and
  refuses a concurrent call rather than queueing it, so the answer is immediate.

- 2026-09-07 — **Enabling `SPEAK_LISTEN` registers with the supervisor at session
  start.** `/talk` runs as its own short-lived process, so without a warm server it
  pays a cold start on every invocation. Registering eagerly trades resident memory
  from session start for an instant `/talk`. Measured on an M5 with the CPU backend on
  2026-09-07, once the server answers `/health`: 780 MB with the voice alone, 1.5 GB
  with the transcription model beside it. Sessions that do not enable listening keep
  today's lazy registration.

- 2026-09-07 — **A running speech server is never replaced, and that is guarded
  twice.** A second server would overwrite `server.json` with a pid that cannot bind
  the port and orphan the first — the last session out would then stop the wrong
  process and leave a gigabyte resident. The session warmup and `/talk` therefore call
  `reachable()` before `ensure_running()`. That check is liveness, not readiness: any
  HTTP answer means the port is taken, including the 503 of a server still warming up
  and the case of a server with no ASR model, neither of which may be replaced.

  `ensure_running` also refuses on its own when `server.json` names a live pid, which
  covers the caller that cannot make the check: the TTS fallback starts the server from
  `except (HTTPError, OSError)`, so a 5xx or a synthesis timeout from a loaded server
  reaches it as "the server is gone". Guarding at the supervisor protects every call
  site at once, including future ones.

- 2026-09-07 — **A cold start is reported as a cold start, not as a missing model.**
  `has_asr` answers False for any HTTP failure, so a server that is absent or still
  loading its models looks exactly like one served without `--asr-model` — and that
  message tells the user to run `/speak:setup` or restart another session, neither of
  which applies to a server that is eight seconds from ready. `SPEAK_LISTEN=1` starts
  the server as the session opens, which makes that window the ordinary first run.
  Three things changed together: `liveness()` answers ABSENT, STARTING or READY off
  the one `/health` request, so both entry points can say "wait"; `ensure_running`
  probes on its early returns as well, so a caller that finds another session's start
  already under way is handed a server that has answered rather than one that has
  merely been recorded; and `listen` waits on its own session's warmup task, which
  until now was created and never awaited. Rejected: leaving `/talk` to infer
  readiness from `has_asr`, which is what produced the wrong advice.

- 2026-09-07 — **Transcription and liveness get different timeouts, and the
  transcription one is derived.** They are the same HTTP client but not the same
  wait: `/health` and `/v1/models` are localhost questions a live server answers in
  milliseconds, while a transcription is minutes of audio through a model. One
  30-second timeout served both, so a wedged server held `listen` for half a minute
  and a recording longer than about a minute could not be transcribed at all — the
  `ReadTimeout` surfaced as "could not transcribe", blaming the server, with the
  audio already gone. The probes now use a short fixed timeout and the transcription
  one comes from the configured recording cap, which is the only number that can
  bound it. Rejected: raising the single constant, which re-breaks the moment
  `SPEAK_LISTEN_MAX_SECONDS` is raised.

- 2026-09-07 — **The queue re-checks the generation after spawning the player.** The
  existing check ran only before the spawn, so an utterance stopped in the window
  between `stop()` releasing the current process and the new handle being recorded was
  still heard. The player is now terminated the moment its handle is stored if the
  generation has moved since. It matters more with voice input than without: `listen`
  opens the microphone as soon as speech is meant to have ended, so an utterance that
  survives that window is recorded as well as heard.

- 2026-09-07 — **Capture uses the `sounddevice` package rather than an ffmpeg
  subprocess.** It is the first native-wheel dependency here, and it is what makes
  voice input work off macOS; an `avfoundation` command line would not. The import is
  inside the recorder so a machine without a working PortAudio still gets speech.
  Rejected: ffmpeg, which is one more thing to install and macOS-specific in this use.

- 2026-09-07 — **A denied microphone is reported as silence, not as an error.** On
  macOS a denied permission commonly opens the stream and delivers zeroed samples
  rather than raising, so no error is available to report. Both entry points therefore
  answer "heard nothing", and the documentation names that as the symptom to look for
  instead of promising a message the user may never see. It is named as the first
  thing to check rather than the only one: "heard nothing" says the gate never
  opened, which a muted device, the wrong input, or a cap shorter than the user's
  pause produce just as well.

- 2026-09-07 — **Whether anyone spoke is recorded by the tracker, not inferred from
  the terminal state.** `SilenceTracker.feed` checks the cap before the start
  timeout, so a recording of an empty room ends CAPPED rather than SILENT whenever
  the cap is the shorter limit — which `listen(max_seconds=8)` produces against the
  default 10 s timeout, and `max_seconds` is a parameter the model chooses. Treating
  CAPPED as speech sent room tone to the transcriber, and a transcription model given
  silence reliably emits a stock phrase: a fabricated answer for `listen`, and an
  injected prompt for `/talk`, whose command file says to act on it as the user's
  request. `Recording.has_speech` is therefore the fact that the gate opened, carried
  from the tracker. Rejected: clamping the start timeout to the cap, which narrows the
  window but still lands on CAPPED when the two are equal.

- 2026-09-07 — **The microphone gate is clamped from above as well as below.**
  Calibration averages the first 300 ms, and nothing cues the user to wait, so
  talking straight away is the ordinary first run rather than a corner case. With no
  upper clamp the gate landed near three times speech level, above anything the
  microphone produces: the recording ended SILENT at the start timeout and answered
  "heard nothing", which the documentation used to attribute to a denied microphone
  alone. A gate that is too low costs a longer recording; one that is too high loses
  the utterance and misdirects the user.
