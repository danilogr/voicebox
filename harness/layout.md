# <img src="../assets/voicebox-detailed.svg" width="28" height="28" alt="" valign="middle" /> Codebase layout

> Every file and what it is for. Architecture and decisions: [architecture.md](architecture.md).

## Directory overview

    .claude-plugin/     plugin manifest and the voicebox-marketplace listing
    assets/             art: voicebox, transcribe and narrate icons and heroes; favicons
    commands/           /voicebox:narrate, :setup, :telemetry, :stats
    harness/            architecture, user flow, performance, design guidelines, this file
    scripts/            the MCP launcher and the local-install helper
    src/voicebox/       the plugin
    tests/              the offline test suite and the vendored telemetry contract

## Root files

| File | Purpose |
|---|---|
| `README.md` | What voicebox does, install, tools, settings, privacy |
| `TELEMETRY.md` | Every telemetry field, the notice, and how to turn it off |
| `AGENTS.md` | Operating rules for coding agents in this repo |
| `pyproject.toml`, `uv.lock` | Package, console scripts (`voicebox-mcp`, `voicebox-telemetry`, `voicebox-stats`), tool settings, the pinned lock |
| `.mcp.json` | Launches the `voicebox` MCP server through `scripts/voicebox-mcp` |
| `.claude-plugin/plugin.json`, `marketplace.json` | The plugin, and the marketplace listing it |
| `favicon.ico`, `favicon.png`, `assets/favicon.*` | Icons; no code reads them |
| `scripts/voicebox-mcp` | Finds `uv` and runs `voicebox-mcp` |
| `scripts/install-local.sh` | Copies this tree into Claude Code's plugin cache for a non-local install |

## `src/voicebox/`

| File | Responsibility |
|---|---|
| `__main__.py` | Composition root; stdio and stop signals |
| `config.py` | `Settings`: shared server, `VOICEBOX_` voice, transcription, state dir |
| `server.py` | The six tools and the `narrate` prompt |
| `tools.py` | Tool descriptions |
| `narration.py` | One telemetry row per reading-aloud run |
| `programs.py` | Running ffprobe and ffmpeg with timeouts |

### <img src="../assets/transcribe-detailed.svg" width="20" height="20" alt="" valign="middle" /> `jobs/` and `transcribe/`

| File | Responsibility |
|---|---|
| `jobs/store.py` | Job records (`TranscriptionJob`, `SynthesisJob`), ids, artifacts, pruning |
| `jobs/lock.py` | One voicebox job at a time |
| `jobs/desk.py` | Claiming the lock, launching a worker, recognising a dead one, the start-of-session sweep |
| `jobs/status.py` | `job_status` |
| `transcribe/service.py` | `transcribe`'s checks and launch |
| `transcribe/worker.py` | The transcription worker |
| `transcribe/probe.py`, `prepare.py`, `output.py` | ffprobe, conversion, output paths and transcript text |

### <img src="../assets/narrate-detailed.svg" width="20" height="20" alt="" valign="middle" /> `synthesize/` and reading aloud

| File | Responsibility |
|---|---|
| `synthesize/service.py` | `synthesize`'s checks, staging and launch |
| `synthesize/worker.py` | The synthesis worker: chunks, server, join, encode, write |
| `synthesize/text.py` | Markdown stripping, paragraphs, chunking |
| `synthesize/audio.py` | Joining WAVs with pauses |
| `synthesize/encode.py` | m4a and mp3 through ffmpeg |
| `synthesize/limits.py` | Measured chunk size, request timeout and throughput |
| `narration.py`, `commands/narrate.md` | Reading aloud's telemetry and its instruction |

### `telemetry/`

| File | Responsibility |
|---|---|
| `schema.sql` | The contract's local schema, vendored byte for byte |
| `contract.py` | Row table, schema, identity, store path |
| `rows.py` | `job_row`: every row inside the contract |
| `jobs.py`, `record.py` | A finished job's row, and storing it |
| `session.py`, `compose.py` | A session's row and connection |
| `notice.py` | The first-run notice |
| `stats.py`, `commands.py` | `voicebox-stats` and `voicebox-telemetry` |

## `tests/`

| Path | What it covers |
|---|---|
| `contract/` | The vendored contract (`CONTRACT.md`, `v1/`), its fixtures, freshness against the voicebox-telemetry checkout, and every client-produced row validated |
| `telemetry/` | Rows, session, composition, notice, stats, commands, and each job kind's rows |
| `test_job_*.py` | Store, lock and status |
| `test_transcribe_*.py`, `test_programs.py` | Transcription |
| `test_synthesis_*.py`, `synthesis_support.py` | Synthesis |
| `test_narration.py`, `test_tool_behaviour.py` | Reading aloud |
| `test_server.py`, `test_stop_signals.py`, `test_shutdown.py`, `test_tools.py` | The server over stdio and in process |
| `test_config.py`, `test_composition.py`, `test_harness_detection.py`, `test_manifests.py` | Settings, wiring, harness detection, the manifests |
