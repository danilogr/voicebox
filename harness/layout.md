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
├── src/speak/              The speak plugin: MCP server, voice input, file transcription
│   ├── assets/             Synthesized WAV cue sounds
│   ├── telemetry/          The plugin's telemetry wiring: contract, event composition, stats and consent commands (storage and reporting live in speak-core)
│   └── transcribe/         Background audio/video file transcription subsystem
├── tests/                  Test suite (460+ offline, mocked tests)
└── voice-tuning/           Tools and dataset script for personalized ASR fine-tuning
```

Engines, supervisor, queue, speaker lock and telemetry mechanism live in speak-core.

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
| [`scripts/install-local.sh`](../scripts/install-local.sh) | Developer utility to sync the current git checkout directly into `~/.claude/plugins/cache/` for immediate testing in Claude Code. |
| [`scripts/make-cues.py`](../scripts/make-cues.py) | Self-contained script using Python's standard `wave` and `math` libraries to synthesize the three shipped 16-bit PCM WAV cue sounds. |

---

## Slash Commands (`commands/`)

Markdown instruction files loaded by Claude Code when a user runs slash commands:

| File | Command | Description |
|---|---|---|
| [`commands/setup.md`](../commands/setup.md) | `/speak:setup` | Guides Claude to bootstrap the neural speech engine, run `speak-core-setup`, check hardware, and configure `SPEAK_ENGINE=nemo`. |
| [`commands/converse.md`](../commands/converse.md) | `/speak:converse` | Puts the session into spoken conversation mode: Claude speaks answers aloud via `speak` and listens for user replies via `listen`. |
| [`commands/narrate.md`](../commands/narrate.md) | `/speak:narrate` | Instructs Claude to read long texts, files, or URLs aloud in controlled 10-sentence rounds without overwhelming the speech queue. |
| [`commands/talk.md`](../commands/talk.md) | `/talk` | Captures user speech via `scripts/speak-talk`, injects the transcript into the prompt, and warns Claude to verify destructive actions. |
| [`commands/telemetry.md`](../commands/telemetry.md) | `/speak:telemetry` | Runs `speak-telemetry` through `uv run` with the user's arguments: shows or changes the telemetry mode, prints the install id, or switches recordings on or off. |
| [`commands/stats.md`](../commands/stats.md) | `/speak:stats` | Runs `speak-stats` through `uv run` to show what telemetry has collected on this machine. |

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
| [`src/speak/settings.py`](../src/speak/settings.py) | This plugin's `Settings`: embeds the server and voice settings and adds the `SPEAK_LISTEN_*`, input device, transcription and state-directory settings. |
| [`src/speak/listening.py`](../src/speak/listening.py) | Capture limits, microphone, cue and transcription timeout, built the same way by `__main__` and `talk`. |

### Microphone Input & Process Coordination

| File | Responsibility |
|---|---|
| [`src/speak/capture.py`](../src/speak/capture.py) | Microphone recording: captures audio from the microphone and stops automatically when silence is detected. |
| [`src/speak/cue.py`](../src/speak/cue.py) | Plays chime sounds when recording starts, stops, or detects no speech. |
| [`src/speak/miclock.py`](../src/speak/miclock.py) | Microphone lock: ensures only one terminal session records audio at a time. |
| [`src/speak/talk.py`](../src/speak/talk.py) | CLI entry point for `/talk`: coordinates microphone recording, transcribes speech, and outputs the result. |
| [`src/speak/assets/`](../src/speak/assets/) | Shipped WAV audio chimes: `cue-open.wav`, `cue-close.wav`, and `cue-empty.wav`. |

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
| [`src/speak/telemetry/contract.py`](../src/speak/telemetry/contract.py) | This plugin's schema: the bundled `schema.sql` plus the column lists, checked against it by a test. |
| [`src/speak/telemetry/compose.py`](../src/speak/telemetry/compose.py) | This plugin's `build()`: `start_install`, its own `sessions` row, and the `TelemetrySession`. |
| [`src/speak/telemetry/session.py`](../src/speak/telemetry/session.py) | Session tracker: records session start and end times. |
| [`src/speak/telemetry/stats.py`](../src/speak/telemetry/stats.py) | This plugin's `speak-stats` rendering: plain SQL over its raw tables. |
| [`src/speak/telemetry/commands.py`](../src/speak/telemetry/commands.py) | The `speak-telemetry` and `speak-stats` console scripts; the only telemetry module reading `os.environ` and `sys.argv`. |

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

460+ automated tests executed with `uv run pytest`:

| Directory / File | What it tests |
|---|---|
| `tests/contract/` | Telemetry wire protocol compliance against JSON schemas and fixtures. |
| `tests/telemetry/` | This plugin's telemetry: its contract, session, composition and stats. |
| `tests/test_capture.py` | Microphone energy gating, calibration, silence timeouts, and device selection. |
| `tests/test_transcribe_*.py` | Media probing, audio conversion, background worker lifecycle, and formatters. |
| `tests/test_server.py` | MCP tool dispatch, parameter validation, and prompt handlers. |
