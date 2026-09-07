# speak

Gives Claude Code a voice. A `speak` tool call says one short sentence out loud —
when a task finishes, when it is blocked, or when it needs an answer — so you can
look away from the terminal without losing the thread.

## Install

```bash
/plugin marketplace add ~/dev/claude-plugins/speak
/plugin install speak@speak-marketplace
```

Restart the session and the four tools below appear as tool calls.

The only prerequisite is [uv](https://docs.astral.sh/uv/). There is no install step:
the plugin syncs its own environment from `uv.lock` on first launch, and uv fetches a
Python 3.12 interpreter if the machine has none.

For the neural voice, run `/speak:setup` once. It installs NeMo-Speech.cpp and pulls
MagpieTTS (about 1.2 GB), then the plugin manages the server for you.

## Tools

| Tool | What it does |
|---|---|
| `speak(text, voice?, rate?)` | Queues one sentence and returns immediately. Utterances play in order and never overlap. |
| `stop_speaking()` | Silences what is playing and drops everything queued behind it. |
| `list_voices()` | The voices the current engine can use, and which one is active. |
| `set_voice(voice)` | Changes the voice for the rest of this session. |

Voice is per connection. Claude Code runs one plugin process per session, so
`set_voice` in one window cannot be seen by another and nothing is written to disk.
Set `SPEAK_VOICE` if you want a different starting voice everywhere.

Text is collapsed to a single line and capped (400 characters by default), so a stray
paragraph can never be read aloud in full.

### A different voice every session

Set `SPEAK_VOICE=random` and each session opens on a voice picked at random, then
keeps it until the session ends. `set_voice` still overrides the roll.

What it draws from depends on the engine. `nemo` uses every speaker the model
reports, which is what you want — all five are real voices. `say` reports 184,
most of them novelty or legacy, so it draws from a curated set of natural voices:
Samantha, Karen, Moira, Tessa, Daniel, Rishi, Aman, Tara. Names missing from your
system are skipped, and if none of them are installed it falls back to every voice
`say` reports.

Override either with `SPEAK_VOICE_POOL="Karen,Moira,Tessa"`, which is taken at its
word and applies to any engine.

## When Claude speaks

The policy lives in the `speak` tool description, which is always in context: finished
a task, blocked and needs input, asking you a question. It is told not to narrate
routine progress and not to read out code or paths.

If it talks too much or too little, edit the description in `src/speak/tools.py` —
that one string is the whole behaviour.

## Configuration

All optional, set in your shell or in the plugin's `.mcp.json`:

| Variable | Default | Meaning |
|---|---|---|
| `SPEAK_ENGINE` | `say` | `say`, `nemo`, or `command` |
| `SPEAK_VOICE` | system voice | e.g. `Samantha`. `say -v '?'` lists 184 of them. Set `random` for a new voice each session |
| `SPEAK_VOICE_POOL` | engine's own pool | comma-separated names the random pick draws from |
| `SPEAK_RATE` | system rate | words per minute; the macOS default is about 175 |
| `SPEAK_MAX_CHARS` | `400` | truncation cap, at a word boundary |
| `NEMO_HTTP_URL` | `http://127.0.0.1:8082` | OpenAI-compatible `/v1/audio/speech` server |
| `NEMO_API_KEY` | — | bearer token for that server |
| `NEMO_TTS_MODEL` | `magpie` | model name sent in the request |
| `SPEAK_COMMAND` | — | required when `SPEAK_ENGINE=command` |
| `SPEAK_NEMO_AUTOSTART` | `1` | set `0` to start and stop the speech server yourself |
| `SPEAK_NEMO_BIN` | `nemo-speech` | binary the supervisor spawns |
| `SPEAK_SEED` | — | passed as `--tts.seed`; pins pacing between runs |
| `SPEAK_STATE_DIR` | `~/Library/Caches/speak` | where the shared-server state lives |
| `SPEAK_UV` | — | absolute path to uv, if the launcher should not search for it |

### Engines

**`say`** (default) — macOS built-in. Offline, instant, always available.

**`nemo`** — a local [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp)
server running MagpieTTS. Run `/speak:setup` once, set `SPEAK_ENGINE=nemo`, and the
plugin does the rest: it starts the server the first time you need speech, shares one
server across every Claude Code window, and stops it when the last one closes.

Voices are `John`, `Sofia`, `Aria`, `Jason`, and `Leo` — read from the loaded model,
not hardcoded. The OpenAI-compatible names (`nova`, `alloy`, …) are accepted but all
resolve to the model's default speaker, so the plugin defaults to `John` explicitly
instead.

Measured on an M5 with the CPU backend: 10 s to start, then about 1.7 s per sentence
against 0.7 s for `say`, holding roughly 1 GB of RSS while up. The first sentence after
a cold start is spoken by `say` while the server warms up, so speech is never delayed
and never silently dropped.

The supervisor always passes `--device cpu`. Metal works, but on Apple silicon it runs
this workload *slower* than the CPU backend — about 1.5x realtime against 2.0x — so CPU
is both the simpler and the faster choice here.

**`command`** — hands the sentence to any binary as its last argument. Useful for
piper, espeak, a shell script, or a remote speaker:

```bash
SPEAK_ENGINE=command SPEAK_COMMAND="/usr/local/bin/piper --model en_US.onnx"
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

145 tests, no network and no audio: the queue runs against a fake spawner, the NeMo
engine against a mock HTTP transport, the supervisor against injected spawn, liveness
and kill callables over a temporary state directory, and the end-to-end tests drive the
real server process over stdio with the `command` engine pointed at a recorder script.
No test starts a speech server or downloads a model.

## Layout

```
.claude-plugin/plugin.json       plugin manifest
.claude-plugin/marketplace.json  so the repo installs as a marketplace
.mcp.json                        registers the stdio server
scripts/speak-mcp                finds uv and launches the server
scripts/install-nemo.sh          idempotent bootstrap, run by /speak:setup
commands/setup.md                the /speak:setup command
harness/architecture.md          system shape and the decisions behind it
src/speak/__main__.py            entry point: environment to objects, then serve
src/speak/config.py              settings, parsed and validated from the environment
src/speak/server.py              the four tools over the queue and the engine
src/speak/tools.py               tool descriptions and the when-to-speak policy
src/speak/queue.py               one process at a time, non-blocking enqueue
src/speak/engines/               say · nemo · command, and their voice lists
src/speak/supervisor.py          starts, shares, and stops the speech server
src/speak/text.py                whitespace collapsing and the length cap
```

## Notes

Speech is per session: the queue and the chosen voice live in the MCP server process
and die with the session, which is the behaviour you want. `stop_speaking` is the
escape hatch when a queued sentence is no longer wanted.

The speech server is the opposite — one per machine, not one per session, because it
costs about a gigabyte. Sessions coordinate through `SPEAK_STATE_DIR`: a lock directory
elects whichever session starts it, and each session leaves a file behind so the last
one out can stop it. Sessions that crash are pruned by pid, so a lost window cannot pin
the memory forever.
