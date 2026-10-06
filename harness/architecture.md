# Architecture

> System shape and the decisions behind it. Update after an agreed
> architectural change. Keep decisions with their rationale (the "why").

## Overview

`speak` is a Claude Code plugin that gives the agent a voice. It runs as an MCP
server over stdio and exposes five tools — `speak`, `stop_speaking`, `list_voices`,
`set_voice`, `speech_status` — so the model can say one short sentence out loud at the
moments where a voice beats scrollback. Its single responsibility is turning a sentence
into audible speech without ever blocking the agent and without letting two utterances
overlap.

It also hears, unconditionally. A sixth tool, `listen`, records the user and
returns a transcript, and the `/talk` slash command dictates a prompt instead of
typing it. There are therefore two entry points into the same code: the long-lived
MCP server (`__main__`) and a short-lived one-shot process (`talk`) that a slash
command runs and whose stdout is injected into the user's message. Both transcribe
against the same shared speech server the supervisor already manages.

The engines, the supervisor, the playback queue, the speaker lock, the shared settings and the telemetry mechanism are [speak-core](https://github.com/danilogr/speak-core)'s, a git dependency pinned by tag; their design and the decisions behind them are in its `harness/architecture.md`. This document covers what this plugin builds on top.

## Components & boundaries

- **`__main__`** — the composition root for the MCP server. Reads the environment,
  builds every other component, and starts the transport. It must not contain speech
  behaviour; the only logic it owns is which components to build at all, in
  `build_ears` and `build_supervisor`, which `tests/test_composition.py` covers. Voice input added a second entry point, so the
  boundary rule now names both: `__main__` and `talk` are the only modules that read
  this plugin's configuration from `os.environ`, and neither installs a log handler
  itself — both call `speak_core.runtime.configure_logging`. One other module touches the
  environment at all: `speak_core.engines.say` reads `LANG` to filter `say -v '?'` by locale,
  which is the platform's setting rather than the plugin's configuration.
- **`settings.Settings`** — this plugin's settings: a `ServerSettings`, a `VoiceSettings`
  read under `SPEAK_`, and the listening and transcription settings, under the same
  rules.
- **`server.create_server`** — the transport layer. Defines the tools inside a
  factory that closes over its dependencies, so there is no module-level mutable state
  and tests can build a server over fakes. It must not contain speech logic; it
  normalizes, delegates, and formats the reply. `listen` is defined only when the
  factory is given an `Ears`, which bundles the recorder, the transcriber, the
  microphone lock, the capture limits and the recording cue into one presence check.
- **`cue`** — the three sounds that bracket one recording: open, close, and the error
  tone that replaces the close when nothing was captured. Played through the resolved
  player with the same detached-spawn helper the speech queue uses. It must not know
  why the microphone is opening, and it must never fail a recording: a missing sound
  file, a missing player or a non-zero exit is a debug log and nothing else.
- **`capture`** — the microphone. `SilenceTracker` is a pure state machine fed block
  energies and decides when an utterance has ended; `MicrophoneRecorder` is a thin
  shell that opens a `sounddevice` stream and feeds it. The split is what lets every
  capture path be tested without audio hardware.
- **`miclock`** — one holder at a time for the single microphone, elected the same
  way the speech server's start lock is. It must not know what the microphone is for.
- **`listening`** — what both of this plugin's entry points build the same way for
  listening: the capture limits, the microphone, the cue and the transcription timeout.
- **`talk`** — the `/talk` one-shot: hold the shared server for the life of the
  process, record, transcribe, print. It is the only module besides `__main__` that
  reads the plugin's configuration from the environment, and the only one where
  printing to stdout is the point.
- **`transcribe`** — file transcription. `service` holds the two tools' logic,
  `probe` and `prepare` read and convert media, `jobs` keeps one JSON record per
  job, and `worker` is the detached process that owns a job end to end. It shares
  no state with the speech server: a transcription cannot stall speech.
## Data model

Almost nothing is persistent.

- **Session state** (in memory, per process): the active voice and rate, whether the
  voice is re-rolled per utterance, the warmup task, and whether this session has
  registered with the supervisor. Claude Code runs one server process
  per session, so this is already isolated from every other window. Deliberately never
  written to disk.
- **Plugin state** (on disk, per machine), under `SPEAK_STATE_DIR` (`<cache>/speak`):
  - `mic/owner` — the pid of the session currently recording. Its directory is the
    lock; the file says who holds it, so a lock left by a dead session is reclaimed.
  - `transcribe/`, `transcriptions/` — background transcription jobs and their output.

Voice input is not persistent by default. A recording lives in memory as WAV bytes for
the length of one transcription and is discarded. It is written to disk only when the user
turns recording on (`record: true` in the telemetry config), in which case each recording is
kept locally beside its utterance row so a user can later train a model on their own voice.
Recordings are never uploaded under any setting — the server schema has no column for them.

## Telemetry

`speak/telemetry/` is this plugin's part of the telemetry, on top of `speak_core.telemetry`
(identity, consent, the machine probe, the store, shipping and the install ping). The rest
of the plugin only reaches it through two seams: the queue's `on_finish` callback (one `UtteranceOutcome` per
utterance that leaves it) and a `TelemetrySession` handed to `create_server` and to
`talk.dictate`. Neither the queue nor the server knows what a `TelemetrySession`
does with what it is given, matching the existing rule that the queue must not know
which engine produced an utterance.

- **`contract.py`** — this plugin's schema; a test checks its column lists against the
  SQL, including every `-- LOCAL` column.
- **`session.py`** — turns queue outcomes and listen/talk attempts into stored rows.
  Every public method catches its own failures: a dead telemetry backend has to be
  indistinguishable from a healthy one.
- **`compose.py`** — this plugin's `build()`: `start_install`, then its own `sessions`
  row, then the `TelemetrySession`. Shared by `__main__` and `talk`.
- **`stats.py`** — `speak-stats`: `stats.render` is this plugin's plain SQL over its
  tables, run through `speak_core.telemetry.report`, which finds the store and prints the
  install id and mode.
- **`commands.py`** — the two console scripts; the only telemetry module that reads
  `os.environ` and `sys.argv`.

Shipping and the ping run once per session, scheduled from `create_server`'s lifespan
rather than from `__main__.build()` itself: `build()` runs before the event loop
starts, so nothing there can call `asyncio.create_task`. `/talk` sends the ping
alongside its recording but never ships; the next MCP session ships its rows.

The lifespan's shutdown is what closes a session row, so `__main__.serve_stdio`
turns SIGTERM and SIGHUP into a cancellation that unwinds through it, as Ctrl-C does.
Left at their defaults, either signal kills the process before any of it runs. Once
that shutdown has run, `__main__.run` ends the process with `os._exit`: the MCP
transport reads stdin on a non-daemon thread no signal interrupts, and a normal exit
would wait for it until the host closed the pipe.

## External dependencies

- **`mcp`** — the official MCP SDK. Provides the stdio transport, protocol negotiation,
  tool-schema generation from type hints, and the client used by the end-to-end tests.
- **`speak-core`** — the shared speech library, `speak-core @ git+https://github.com/danilogr/speak-core@<tag>`.
- **`sounddevice`** — the microphone binding, and the only native-wheel dependency
  here. Imported inside the recorder rather than at module scope, so a machine without
  a working PortAudio still runs the speech half of the plugin.
- **`uv`** — environment and dependency management, and the launcher. Must be
  resolvable at startup; `scripts/speak-mcp` finds it.
- **macOS `say`** — the default engine, and the fallback whenever the speech server
  cannot be reached. macOS-only, which is the one part of the plugin that is: on any
  other platform it is reported once at startup, and `SPEAK_ENGINE=nemo` or
  `SPEAK_ENGINE=command` is the way through.
- **A WAV player** — discovered, not assumed: `afplay`, PowerShell's `SoundPlayer`, or
  one of `paplay`, `aplay`, `ffplay`, `play`, `mpv`. Not a hard dependency; with none
  present the cues are skipped and the `nemo` engine speaks through the fallback rather
  than writing audio nothing can play.
- **The cue sounds** — synthesised by `scripts/make-cues.py` and committed under
  `src/speak/assets`. Not taken from the host operating system: the paths differ per
  platform, and shipping another vendor's audio is not ours to do.
- **NVIDIA NeMo-Speech.cpp** (https://github.com/NVIDIA/NeMo-Speech.cpp, optional) — the
  local neural speech server: MagpieTTS for speech, Nemotron 3.5 for recognition.
  Installed by `speak-core-setup` and managed by the supervisor. Not vendored: it is a
  per-platform binary plus both models, which is over a gigabyte of download. Sizes
  are in `performance.md`.

## Key decisions (with rationale)

- **The wire carries no plugin name.** Contract v1 fixes the ping at three fields
  (`plugin_version`, `os`, `event`) and the transport sends no user agent, so a backend
  cannot tell one plugin's ping from another's. Each plugin has its own telemetry
  Worker, so nothing needs splitting by product. A plugin's identity
  (`TelemetryIdentity`) steers only the client: its variables, config directory,
  endpoint and disclosure version.

- **The when-to-speak policy lives in the `speak` tool description.**
  A tool description is always in the model's context at the moment the choice is made.
  Not a hook or a skill, neither of which can add a callable tool or is guaranteed
  to be read at that moment.

  The closing one-line recap is part of that policy and lives in the same place, for the
  same reason plus one more: a `Stop` hook receives a shell command and no view of the
  turn, so it can play a fixed sound but cannot say what changed. The hook types that
  could summarise — `prompt` and `agent` — are restricted to tool events. Only the model
  can write the recap. Nor per-user memory or `AGENTS.md`, which would make
  the plugin's own behaviour depend on machine-local state that does not travel with it.

- **Voice and rate are per connection and never persisted.** One server
  process per session means a plain attribute is already isolated. Not a
  machine-wide voice file, which would leak one window's choice into every other.

- **Dependencies are injected, and the server is built by a factory.**
  `create_server` closes over its engine, queue, and supervisor; the queue takes its
  process launcher, and the supervisor its spawn, liveness, and kill callables. This is
  what lets the suite cover the real code paths with no audio, no sockets, and no
  speech server. Not module-level singletons, which would force monkeypatching.

- **Deliberate tool failures raise `ToolError`.** The SDK withholds a raw
  exception's text from the model and sends a generic message instead, which would turn
  "unknown voice X; available: …" into unusable feedback. `ToolError` carries the
  message through.

- **A random session voice is resolved lazily, not at startup.** A
  speech server reports no voices until it has warmed up, so rolling at construction
  would always fall back to the default. The roll happens on first use and retries
  until the engine can answer. Per engine, the eligible pool differs: `nemo` uses every
  speaker the model reports, while `say` reports mostly novelty and legacy voices and so
  draws from a curated natural-voice list, narrowed to what is actually installed.
  `SPEAK_VOICE_POOL` overrides both and is taken at its word, so it also works with an
  engine that reports no names at all.

- **Shutdown silences the queue before tearing down its worker.** The
  worker task owns the running child process, so cancelling it first drops the handle
  and leaves the machine talking after the session is gone. Stopping the queue first
  terminates the child while it is still tracked.

- **stdout is the protocol channel.** All logging goes to stderr, and the
  log handler is installed in one place. A stray `print` in library code desynchronizes
  the session, so library modules use `logging.getLogger(__name__)` only.

  `/talk` is the exception that proves it: its stdout is a prompt, not a protocol,
  and a failure there is printed to stdout as well as stderr. `commands/talk.md`
  branches on a line beginning `speak:`, and only stdout is certainly injected —
  whether a slash command's `!` capture carries stderr from a non-zero exit is not
  this plugin's to guarantee, and if it does not, stderr alone leaves the model an
  empty transcript under an instruction to act on it as the user's request. The
  prefix is what keeps the stdout copy from being read as speech.

- **`.mcp.json` launches `/bin/sh scripts/speak-mcp` rather than `uv`.**
  `/bin/sh` is an absolute path present on every target system, so the launch has no
  PATH dependency; the shim then finds uv via `$SPEAK_UV`, the PATH, and the locations
  uv's installers use. Not invoking `uv` directly, which fails under any parent
  that does not source the user's shell profile.

- **`listen` drains the speech queue before opening the microphone.**
  `speak` returns immediately by design, so a question is usually still playing when
  the next tool call runs, and without the drain the recording captures the agent's
  own voice. `speak_core.queue.SpeechQueue.drain()` exists for this and waits out synthesis as well as
  playback, and it waits as long as utterances keep finishing: one spoken answer is
  several of them, and each costs its synthesis as well as its audio, so a batch runs
  for minutes of legitimate work. Only a stall - nothing finishing inside a minute -
  ends the wait, and then the call is refused rather than recorded. Our own voice handed
  back as the user's answer is a fabricated request the model acts on, where a refusal
  can simply be retried.
  Not a combined `ask(question)` tool, which duplicates `speak` to buy only the
  ordering the drain already gives.

  `/talk` has none of this and cannot: it is a separate process with no handle on
  the session's queue, and a cross-process "is anything playing" signal costs more
  than the problem is worth. So a `/talk` started mid-sentence records the agent's
  voice, and `commands/talk.md` says to act on the result as the user's request.
  Documented in the README as a thing to wait out rather than engineered around.

- **Recording stops on silence, not on a keypress.** Neither entry point
  has a terminal: the MCP server owns stdin for the protocol, and a slash command's
  bash output is captured. There is no key to press, so silence is the only available
  end-of-turn signal. The gate is calibrated from the room's own ambient level over
  the first 300 ms rather than fixed, because a fixed threshold fails on a quiet
  microphone and in a noisy room alike.

- **`--asr-model` is only added when listening is enabled.** A missing
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

- **The microphone is arbitrated by a lock, and contention fails fast.**
  There is one microphone and any number of sessions; two recordings at once produce
  garbage in both. The election is the supervisor's, reused: an atomic `mkdir` on
  `mic/`, an owner pid in the file inside it, and a lock left by a dead session
  reclaimed by the next caller. Not queueing on the lock, which is worse than
  refusing — you would start talking and a window you are not looking at would take
  the words.

- **A second, in-process lock guards `listen` against itself.** The
  microphone lock is re-entrant by pid, which is what makes reclaiming a dead owner's
  lock possible but means it does not separate two calls inside one session: both
  would acquire it, and the first to finish would release the cross-process lock while
  the other was still recording. `listen` therefore also holds an `asyncio.Lock` and
  refuses a concurrent call rather than queueing it, so the answer is immediate.

- **A session registers with the supervisor at session start.** `/talk` runs as its own short-lived process, so without a warm server it
  pays a cold start on every invocation. Registering eagerly trades resident memory
  from session start for an instant `/talk`; the transcription model roughly doubles
  what the server holds, and `performance.md` has the figures. Sessions that do not
  enable listening register lazily instead.

- **A running speech server is never replaced, and the listening paths
  guard it twice.** A second server would overwrite `server.json` with a pid that
  cannot bind the port and orphan the first — the last session out would then stop
  the wrong process and leave a gigabyte resident.

  The two listening paths, the session warmup and `/talk`, call `reachable()` before
  `ensure_running()`. That check is liveness, not readiness: any HTTP answer means
  the port is taken, including the 503 of a server still warming up and the case of a
  server with no ASR model, neither of which may be replaced.

  Every path, those two included, is then guarded again inside `ensure_running`,
  which refuses when `server.json` names a live pid. That is the only guard on the
  TTS fallback (`__main__.start_speech_server`), which cannot make the first check:
  it starts the server from `except (HTTPError, OSError)`, so a 5xx or a synthesis
  timeout from a loaded server reaches it as "the server is gone". The pid check is
  the weaker of the two — it trusts `server.json`, so a record whose pid the OS has
  since reused reads as a live server — but guarding at the supervisor covers every
  call site at once, including future ones.

- **A cold start is reported as a cold start, not as a missing model.**
  `has_asr` answers False for any HTTP failure, so a server that is absent or still
  loading its models looks exactly like one served without `--asr-model` — and that
  message tells the user to run `/speak:setup` or restart another session, neither of
  which applies to a server that is eight seconds from ready. The server is started as
  the session opens, which makes that window the ordinary first run.
  Three pieces hold it together, and all three are required: `liveness()` answers
  ABSENT, STARTING or READY off one `/health` request, so both entry points can say
  "wait"; `ensure_running` probes on its early returns too, so a caller that finds
  another session's start already under way is handed a server that has answered and
  not merely one that has been recorded; and `listen` awaits its own session's warmup
  task. Readiness must not be inferred from `has_asr`, which answers False for any HTTP
  failure and so cannot tell a loading server from one with no model.

- **Transcription and liveness get different timeouts, and the
  transcription one is derived.** They are the same HTTP client but not the same
  wait: `/health` and `/v1/models` are localhost questions a live server answers in
  milliseconds, while a transcription is minutes of audio through a model. A single
  value cannot serve both: short enough for a probe will not transcribe a long
  recording, and long enough to transcribe holds `listen` for that whole time against
  a server that has wedged. A transcription that times out has already discarded its
  audio and blames the server rather than reporting that it gave up. So the probes take
  a short fixed timeout and the transcription timeout is derived from the configured
  recording cap, which is the only number that can bound it. A single raised constant
  breaks again the moment `SPEAK_LISTEN_MAX_SECONDS` goes up.

- **Capture uses the `sounddevice` package rather than an ffmpeg
  subprocess.** It is the first native-wheel dependency here, and it is what makes
  voice input work off macOS; an `avfoundation` command line would not. The import is
  inside the recorder so a machine without a working PortAudio still gets speech.
  Not ffmpeg, which is one more thing to install and macOS-specific in this use.

- **A denied microphone arrives as silence, not as an error.** macOS opens the stream
  and delivers zeroed samples rather than raising, so there is no error to report and
  no way to distinguish a denied input from a muted one at the API. Both read as exact
  digital silence, which the deafness check treats as a muted device and names as such
  — permission is one of the causes that message lists. "Heard nothing" is reserved for
  a live microphone whose gate never opened, so the two remedies stay separate.

- **Whether anyone spoke is recorded by the tracker, not inferred from
  the terminal state.** `SilenceTracker.feed` checks the cap before the start
  timeout, so a recording of an empty room ends CAPPED rather than SILENT whenever
  the cap is the shorter limit — which `listen(max_seconds=8)` produces against the
  default 10 s timeout, and `max_seconds` is a parameter the model chooses. Treating
  CAPPED as speech would send room tone to the transcriber, and a transcription model
  given silence reliably emits a stock phrase: a fabricated answer for `listen`, and an
  injected prompt for `/talk`, whose command file says to act on it as the user's
  request. `Recording.has_speech` is therefore the fact that the gate opened, carried
  from the tracker. Clamping the start timeout to the cap is not enough: it narrows the
  window but still lands on CAPPED when the two are equal.

- **The microphone gate is clamped from above as well as below.**
  Calibration averages the first 300 ms, and nothing cues the user to wait, so
  talking straight away is the ordinary first run rather than a corner case. Without an
  upper clamp the gate lands near three times speech level, above anything a microphone
  produces, and the recording ends SILENT at the start timeout with nothing to show for
  it. A gate that is too low costs a longer recording; one that is too high loses the
  utterance and misdirects the user.

- **The default input is tried first, and a silent device is stepped
  over.** A stream of exact zeros is not a quiet room: a live microphone always carries
  a noise floor, so digital silence means a muted or disabled input. The OS default
  leads the search, so the device chosen in system settings wins on the first recording
  of every session; whatever answers is kept for the rest of the session, in memory
  only. Not persisting the choice, which would let a remembered device silently
  override the setting the user can see. A device is skipped only for exact silence,
  never for being quiet, or a pause would cascade through every input at ten seconds
  each.

- **Deafness is judged on its own window, not the calibration window.**
  CoreAudio hands over exact zeros while an input primes, and a Bluetooth microphone
  can take most of a second, so a 300 ms verdict rejects a working device that is
  merely slow. A device gets 1.5 s to produce anything; calibration then runs from its
  first real sample, which also keeps priming silence out of the ambient average — a
  gate diluted by zeros lands at the floor, and in a noisy room a floor-level gate
  opens on the room and never closes.

- **A word spoken before the gate arms starts the utterance.** Calibration runs for
  300 ms from the first real sample, and a one-word answer can be over before it ends.
  Averaging that voice into the ambient puts the gate above the speech it exists to
  detect, and the blocks are already buffered, so a block at or above the ceiling ends
  calibration instead: the gate takes the floor and the utterance begins there. The
  ceiling is the dividing line because no room delivers a block that loud, while a noisy
  room can still push the computed gate above it.

- **An empty capture ends on a different sound.** Recording stops on
  silence whether or not anyone spoke, so without a distinct tone the two are
  indistinguishable until the transcript fails to arrive. It replaces the closing cue
  rather than following it: "got it" then "that failed" is worse than only the second.

- **The cue sounds ship with the plugin, and the player is resolved.** A path into a
  system sound directory or a fixed player binary would tie playback to one operating
  system, while the microphone half is portable. Generated sounds are reproducible and
  carry no one else's assets; a resolved player keeps playback a subprocess, which the
  queue needs because it stops an utterance by terminating it.

- **Conversation mode is served as an MCP prompt, from its command file.** Claude Code
  reads `commands/*.md` itself; every other MCP client sees only what the server
  offers. The `converse` prompt serves that same file with its frontmatter removed, and
  takes its description from the frontmatter, so both have one copy whichever app asks.
  It is read once at startup, like the tool descriptions, and a checkout without the
  file loses the prompt and nothing else. It is offered only when `listen` is
  registered, because its instruction is to call it. `/talk` stays a slash command: it
  records before the user's message exists, and a prompt handler blocked on a
  microphone for seconds is what a client's timeout cuts off.

- **The microphone is cued open and cued shut, and the opening cue is
  awaited.** Nothing else told the user the microphone was open; `/talk` is the acute
  case, where you press enter and have no way to tell whether to talk now or in two
  seconds. The closing cue is not decoration either: recording ends on silence rather
  than on a keypress, so without it you never learn when the gate decided you had
  finished. Both are awaited to completion. That is a correctness requirement for the
  opening one and not merely tidiness: `SilenceTracker` calibrates its gate from the
  first 300 ms of audio, so a chime still sounding when the stream opens is averaged
  into the ambient level, sets the gate above what a voice reaches, and ends the
  recording in silence — the failure the upper clamp was added to survive. The clamp
  stays as the backstop; the cue is not allowed to lean on it. The end cue is awaited
  too, because the latency costs nothing next to the transcription that follows.

  The cues bracket the recording alone: they sound after the microphone lock is held
  and the speech server has answered, so a refused or unserviceable `listen` stays
  silent, and the closing one sounds when recording stops rather than when the
  transcript arrives. A cue is a convenience and a recording is not, so a missing
  sound file, a missing player, or a player that exits non-zero degrades to a debug
  log. Not playing the cue without awaiting it, which is exactly the
  calibration bug above; and a cue on only one end, which leaves the more useful half
  of the question — am I still being recorded? — unanswered.
