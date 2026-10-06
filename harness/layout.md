# Codebase Layout

> Exhaustive guide to the files and directories in this repository.
> For system architecture, component boundaries, and design decisions, see [architecture.md](architecture.md).

## Directory Overview

```
.
├── .claude-plugin/         Plugin manifests and marketplace registration
├── commands/               Claude Code slash commands (/speak:setup, /talk, etc.)
├── harness/                Developer specs: architecture, flows, performance, guidelines
├── scripts/                Shell launchers, cue sound synthesis, install helpers
├── src/speak/              Core Python implementation
│   ├── assets/             Synthesized WAV cue sounds
│   ├── engines/            Speech synthesis and ASR engine implementations
│   ├── telemetry/          Local metrics storage and optional anonymous reporting
│   └── transcribe/         Background audio/video file transcription subsystem
├── tests/                  Test suite (770+ offline, mocked tests)
└── voice-tuning/           Tools and dataset script for personalized ASR fine-tuning
```

---

## Root Files

| File | Purpose |
|---|---|
| [`README.md`](../README.md) | High-level project introduction, installation, available tools, commands, configuration, and developer quickstart. |
| [`AGENTS.md`](../AGENTS.md) | Operating rules and behavioral constraints for AI coding agents working on this repository. |
| [`TELEMETRY.md`](../TELEMETRY.md) | Complete documentation of all telemetry fields, privacy guarantees, database schema, and opt-out commands. |
| [`pyproject.toml`](../pyproject.toml) | Python project configuration, dependencies, CLI entry points (`speak-mcp`, `speak-talk`, `speak-telemetry`, `speak-stats`), and tool settings for Ruff, Mypy, and Pytest. |
| [`uv.lock`](../uv.lock) | Pinned dependency lockfile ensuring reproducible builds across all environments. |

---

## Plugin Manifests & Launchers

| File | Purpose |
|---|---|
| [`.claude-plugin/plugin.json`](../.claude-plugin/plugin.json) | Claude Code plugin manifest defining the plugin name, version, description, author, repository, and registered slash commands. |
| [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json) | Local marketplace registry configuration allowing this repository to be installed via `/plugin marketplace add`. |
| [`.mcp.json`](../.mcp.json) | Stdio MCP server registration file declaring how Claude Code launches the `speak` server using `scripts/speak-mcp`. |
| [`scripts/speak-mcp`](../scripts/speak-mcp) | Main launcher executable: discovers `uv` on the host, syncs the virtual environment, and executes the `speak-mcp` entry point. |
| [`scripts/speak-talk`](../scripts/speak-talk) | One-shot executable for prompt dictation: runs `speak-talk` to capture microphone audio and print the transcript to stdout. |
| [`scripts/install-nemo.sh`](../scripts/install-nemo.sh) | Idempotent setup script invoked by `/speak:setup`: downloads `nemo-speech` binary (>= 0.2.0), fetches MagpieTTS and Nemotron-3.5 models (~1.4 GB), and verifies the environment. |
| [`scripts/install-local.sh`](../scripts/install-local.sh) | Developer utility to sync the current git checkout directly into `~/.claude/plugins/cache/` for immediate testing in Claude Code. |
| [`scripts/make-cues.py`](../scripts/make-cues.py) | Self-contained script using Python's standard `wave` and `math` libraries to synthesize the three shipped 16-bit PCM WAV cue sounds. |

---

## Slash Commands (`commands/`)

Markdown instruction files loaded by Claude Code when a user runs slash commands:

| File | Command | Description |
|---|---|---|
| [`commands/setup.md`](../commands/setup.md) | `/speak:setup` | Guides Claude to bootstrap the neural speech engine, run `install-nemo.sh`, check hardware, and configure `SPEAK_ENGINE=nemo`. |
| [`commands/converse.md`](../commands/converse.md) | `/speak:converse` | Puts the session into spoken conversation mode: Claude speaks answers aloud via `speak` and listens for user replies via `listen`. |
| [`commands/narrate.md`](../commands/narrate.md) | `/speak:narrate` | Instructs Claude to read long texts, files, or URLs aloud in controlled 10-sentence rounds without overwhelming the speech queue. |
| [`commands/talk.md`](../commands/talk.md) | `/talk` | Captures user speech via `scripts/speak-talk`, injects the transcript into the prompt, and warns Claude to verify destructive actions. |

---

## Harness Specifications (`harness/`)

System specifications and engineering guidelines:

| File | Description |
|---|---|
| [`harness/architecture.md`](architecture.md) | Architectural boundaries, component responsibilities, state handling, and documented rationales for all key design decisions. |
| [`harness/user-flow.md`](user-flow.md) | End-to-end request/response flows, audio playback lifecycle, silence detection state machine, and microphone device selection. |
| [`harness/performance.md`](performance.md) | Measured benchmarks on Apple Silicon: synthesis and transcription latency, resident memory footprints, disk sizes, and warmup delays. |
| [`harness/integration.md`](integration.md) | Guide for integrating the local speech stack into external agents, custom loops, or OpenAI-compatible client libraries. |
| [`harness/design-guidelines.md`](design-guidelines.md) | Coding standards, Python 3.12+ type conventions, Ruff/Mypy/Pytest rules, logging conventions, and testing requirements. |
| [`harness/layout.md`](layout.md) | This file: complete index and responsibility map of all codebase files. |

---

## Core Application (`src/speak/`)

### Server & Lifecycle

| File | Responsibility |
|---|---|
| [`src/speak/__main__.py`](../src/speak/__main__.py) | Composition root: loads environment settings, initializes dependencies, and starts the MCP server over stdio. |
| [`src/speak/server.py`](../src/speak/server.py) | Defines the tools (`speak`, `listen`, `transcribe`, etc.) and the `converse` prompt registered with the MCP client. |
| [`src/speak/tools.py`](../src/speak/tools.py) | Tool metadata, parameter schemas, and the guidance prompt that tells Claude when to speak. |
| [`src/speak/config.py`](../src/speak/config.py) | Settings every plugin reads the same way: shared `NEMO_*` server settings, a prefixed voice, the plugin and shared state directories. |
| [`src/speak/settings.py`](../src/speak/settings.py) | This plugin's `Settings`: embeds the server and voice settings and adds the `SPEAK_LISTEN_*`, input device, transcription and state-directory settings. |
| [`src/speak/runtime.py`](../src/speak/runtime.py) | Logging for the plugin's and the core's loggers, the speech server health probe, and the supervisor factory. |
| [`src/speak/listening.py`](../src/speak/listening.py) | Capture limits, microphone, cue and transcription timeout, built the same way by `__main__` and `talk`. |

### Audio Output & Playback

| File | Responsibility |
|---|---|
| [`src/speak/queue.py`](../src/speak/queue.py) | Serial speech queue: plays sentences in order without overlapping, prepares the next sentence while one plays, and handles cancellations. |
| [`src/speak/text.py`](../src/speak/text.py) | Text formatting: collapses multi-line text to a single line and caps sentences at word boundaries (default 400 characters). |
| [`src/speak/player.py`](../src/speak/player.py) | Finds an installed audio player command for the current platform (`afplay` on macOS, PowerShell on Windows, `paplay`/`aplay`/`ffplay` on Linux). |

### Microphone Input & Process Coordination

| File | Responsibility |
|---|---|
| [`src/speak/capture.py`](../src/speak/capture.py) | Microphone recording: captures audio from the microphone and stops automatically when silence is detected. |
| [`src/speak/cue.py`](../src/speak/cue.py) | Plays chime sounds when recording starts, stops, or detects no speech. |
| [`src/speak/miclock.py`](../src/speak/miclock.py) | Microphone lock: ensures only one terminal session records audio at a time. |
| [`src/speak/exclusive.py`](../src/speak/exclusive.py) | Cross-process lock using filesystem directories, with automatic cleanup if a process exits unexpectedly. |
| [`src/speak/speaker.py`](../src/speak/speaker.py) | Cross-process speaker lock: one utterance plays at a time across every process that speaks, with turns that alternate. |
| [`src/speak/supervisor.py`](../src/speak/supervisor.py) | Speech server supervisor: starts, shares, and shuts down the local NeMo speech server across open terminal windows. |
| [`src/speak/talk.py`](../src/speak/talk.py) | CLI entry point for `/talk`: coordinates microphone recording, transcribes speech, and outputs the result. |
| [`src/speak/assets/`](../src/speak/assets/) | Shipped WAV audio chimes: `cue-open.wav`, `cue-close.wav`, and `cue-empty.wav`. |

---

## Speech Engines (`src/speak/engines/`)

Pluggable synthesis and transcription backends:

| File | Description |
|---|---|
| [`src/speak/engines/base.py`](../src/speak/engines/base.py) | Core engine protocol (`Engine`) and data structures (`Utterance`, `Voice`) defining the synthesis interface. |
| [`src/speak/engines/say.py`](../src/speak/engines/say.py) | macOS native speech engine using the built-in `/usr/bin/say` executable. Supports voice listing and filtering by locale. |
| [`src/speak/engines/nemo.py`](../src/speak/engines/nemo.py) | Client for local NeMo-Speech.cpp HTTP server: synthesizes speech via MagpieTTS and manages temporary WAV files for playback. |
| [`src/speak/engines/command.py`](../src/speak/engines/command.py) | Custom command engine: invokes any arbitrary command line (e.g., Piper, espeak-ng) passing the text as an argument. |
| [`src/speak/engines/asr.py`](../src/speak/engines/asr.py) | Speech-to-text client sending captured microphone audio to the shared NeMo server via OpenAI-compatible `/v1/audio/transcriptions`. |

---

## File Transcription Subsystem (`src/speak/transcribe/`)

Handles asynchronous transcription of local audio and video files:

| File | Responsibility |
|---|---|
| [`src/speak/transcribe/service.py`](../src/speak/transcribe/service.py) | Service interface: validates file transcription requests, schedules background jobs, and queries status. |
| [`src/speak/transcribe/worker.py`](../src/speak/transcribe/worker.py) | Background worker: extracts audio, runs local speech recognition, and writes output files. |
| [`src/speak/transcribe/jobs.py`](../src/speak/transcribe/jobs.py) | Job store: saves job metadata and progress to JSON records under the cache directory. |
| [`src/speak/transcribe/probe.py`](../src/speak/transcribe/probe.py) | Media inspection: checks file duration and audio streams using `ffprobe`. |
| [`src/speak/transcribe/prepare.py`](../src/speak/transcribe/prepare.py) | Audio converter: extracts and converts media to 16 kHz mono WAV using `ffmpeg` when needed. |
| [`src/speak/transcribe/lock.py`](../src/speak/transcribe/lock.py) | Single-job lock: ensures only one file transcription runs at a time. |
| [`src/speak/transcribe/commands.py`](../src/speak/transcribe/commands.py) | Command generator for external tools (`ffmpeg`, `ffprobe`, `nemo-speech transcribe`). |
| [`src/speak/transcribe/output.py`](../src/speak/transcribe/output.py) | Output formatter: converts recognition results into plain text, JSON with word timestamps, SRT, or VTT. |

---

## Telemetry Subsystem (`src/speak/telemetry/`)

Manages local metrics collection and optional anonymous telemetry reporting:

| File | Responsibility |
|---|---|
| [`src/speak/telemetry/schema.sql`](../src/speak/telemetry/schema.sql) | SQLite schema for local database tracking sessions and performance. Columns marked `-- LOCAL` are never transmitted. |
| [`src/speak/telemetry/schema.py`](../src/speak/telemetry/schema.py) | Schema types: `RowTable` and `TelemetrySchema`, what the table-agnostic store and shipper need to know of a plugin's tables. |
| [`src/speak/telemetry/contract.py`](../src/speak/telemetry/contract.py) | This plugin's schema: the bundled `schema.sql` plus the column lists, checked against it by a test. |
| [`src/speak/telemetry/store.py`](../src/speak/telemetry/store.py) | SQLite client: handles database migrations and records local session events. |
| [`src/speak/telemetry/wiring.py`](../src/speak/telemetry/wiring.py) | Instrumentation: connects server and queue lifecycle events to metrics recording. |
| [`src/speak/telemetry/session.py`](../src/speak/telemetry/session.py) | Session tracker: records session start and end times. |
| [`src/speak/telemetry/ship.py`](../src/speak/telemetry/ship.py) | Background uploader: sends unsent anonymous metrics (with all local-only fields stripped) to the server. |
| [`src/speak/telemetry/ping.py`](../src/speak/telemetry/ping.py) | Anonymous install ping: sends version, OS, and event type on install or upgrade. |
| [`src/speak/telemetry/consent.py`](../src/speak/telemetry/consent.py) | Consent manager: implements the `speak-telemetry` CLI to switch between `on`, `local`, and `off`. |
| [`src/speak/telemetry/report.py`](../src/speak/telemetry/report.py) | CLI viewer: implements `speak-stats` to display summary tables of local metrics. |
| [`src/speak/telemetry/machine.py`](../src/speak/telemetry/machine.py) | System probe: detects OS, CPU core counts, bucketed RAM (e.g. 16, 32 GB), and GPU acceleration. |

---

## Voice Tuning (`voice-tuning/`)

Personalized speech recognition fine-tuning toolkit:

| File | Purpose |
|---|---|
| [`voice-tuning/record.py`](../voice-tuning/record.py) | Interactive CLI recording tool for macOS: guides users through multi-pass recordings of 70 test sentences and generates NeMo-compatible JSONL manifests. |
| [`voice-tuning/sentences.txt`](../voice-tuning/sentences.txt) | 70 curated sentences spanning phonetic balance (Harvard sentences), accent markers, programming jargon, and spoken code identifiers. |
| [`voice-tuning/README.md`](../voice-tuning/README.md) | Step-by-step tutorial for recording personal voice datasets and fine-tuning Nemotron 3.5 ASR models using adapters on an NVIDIA GPU. |

---

## Test Suite (`tests/`)

770+ automated tests executed with `uv run pytest`:

| Directory / File | What it tests |
|---|---|
| `tests/contract/` | Telemetry wire protocol compliance against JSON schemas and fixtures. |
| `tests/telemetry/` | Telemetry storage, consent states, hardware probes, ping delivery, and batch upload mechanisms. |
| `tests/test_engines_*.py` | Synthesis engines (`say`, `nemo`, `command`) under normal, degraded, and fallback conditions. |
| `tests/test_speaker.py` | The speaker lock: waiting, immediate stop, dead-holder reclaim, alternating turns, and an unusable directory. |
| `tests/test_queue.py` | FIFO playback order, depth-1 prefetch pipelining, and `stop_speaking` cancellation. |
| `tests/test_capture.py` | Microphone energy gating, calibration, silence timeouts, and device selection. |
| `tests/test_supervisor.py` | NeMo server process management, election locks, PID tracking, and graceful shutdowns. |
| `tests/test_transcribe_*.py` | Media probing, audio conversion, background worker lifecycle, and formatters. |
| `tests/test_server.py` | MCP tool dispatch, parameter validation, and prompt handlers. |
