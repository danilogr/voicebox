# speak

Gives Claude Code a voice. A `speak` tool call says one short sentence out loud —
when a task finishes, when it is blocked, or when it needs an answer — so you can
look away from the terminal without losing the thread.

Turn on `SPEAK_LISTEN=1` and it gets an ear too: a `listen` tool Claude calls to
hear your answer, and a `/talk` command for saying your next prompt instead of
typing it.

## Install

```bash
/plugin marketplace add ~/dev/claude-plugins/speak
/plugin install speak@speak-marketplace
```

Restart the session and the tools below appear as tool calls.

The only prerequisite is [uv](https://docs.astral.sh/uv/). There is no install step:
the plugin syncs its own environment from `uv.lock` on first launch, and uv fetches a
Python 3.12 interpreter if the machine has none.

For the neural voice, run `/speak:setup` once. It installs NeMo-Speech.cpp and pulls
MagpieTTS and the transcription model — 1.2 GB together, measured — then the plugin
manages the server for you.

## Tools

| Tool | What it does |
|---|---|
| `speak(text, voice?, rate?)` | Queues one sentence and returns immediately. Utterances play in order and never overlap. |
| `stop_speaking()` | Silences what is playing and drops everything queued behind it. |
| `list_voices()` | The voices the current engine can use, and which one is active. |
| `set_voice(voice)` | Changes the voice for the rest of this session. |
| `listen(max_seconds?)` | Records you through the microphone and returns what you said. Waits for anything queued to finish playing first — `/talk`, in its own process, cannot. |

`listen` only exists when `SPEAK_LISTEN=1`; the other four are always there.

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

## When you speak

Voice input is off until you set `SPEAK_LISTEN=1`. It needs the neural setup —
`/speak:setup` pulls a transcription model alongside the voice — because the same
local server does both.

Two ways in:

- **`/talk`** — say your prompt instead of typing it. The transcript is injected
  into your message, prefixed with a note that it came from a local speech model
  and may contain errors, so Claude reads a garbled word charitably and confirms
  before anything destructive. It runs as its own process with no access to the
  session's speech queue, so unlike `listen` it does not wait for Claude to stop
  talking: if a sentence is still playing, the recording will contain it and the
  command file says to act on the result as your request. Let the voice finish
  before you start, or ask Claude to stop speaking first.
- **`listen`** — a tool Claude calls itself. It asks you something with `speak`,
  waits for the sentence to finish playing, then opens the microphone and hears
  your answer.

Recording stops when you stop talking: the gate calibrates to the room's ambient
noise over the first 300 ms, then ends the utterance after 3 seconds of silence.
The gate is clamped at both ends, so starting to talk during that first 300 ms
cannot set it above what a voice reaches and lose the sentence.
Tune it with `SPEAK_LISTEN_SILENCE_MS`, `SPEAK_LISTEN_MAX_SECONDS` (240 by default,
a hard cap), and `SPEAK_LISTEN_TIMEOUT` (10 seconds, how long it waits for you to
start).

There is one microphone, so one session at a time holds it. A second session that
tries to listen is told which one has it rather than being queued behind it —
otherwise you would start talking and a window you are not looking at would take
the words.

`SPEAK_LISTEN=1` also changes when the speech server starts. A session that can
listen claims it as the session opens rather than at the first sentence, because
`/talk` runs as its own short-lived process and would otherwise pay a ten-second
cold start every time. The trade is that the memory is held from session start:
measured on an M5 with the CPU backend, 1.5 GB of resident memory once the server
answers, against 780 MB for a voice-only server.

The first recording asks macOS for microphone access for the app running Claude
Code — the terminal, not Claude Code itself. Expect no error if you say no: a
denied microphone usually opens and returns silence rather than failing, so it
answers "heard nothing".

"heard nothing" means only that the gate never opened, so a denied microphone is
the first thing to check, not the only one. A muted microphone, the wrong input
device, or a `max_seconds` short enough to run out before you started talking all
end the same way. Check the permission in System Settings > Privacy & Security >
Microphone, then the input device and its level.

## Configuration

All optional, set in your shell or in the plugin's `.mcp.json`:

| Variable | Default | Meaning |
|---|---|---|
| `SPEAK_ENGINE` | `say` | `say`, `nemo`, or `command` |
| `SPEAK_VOICE` | system voice | e.g. `Samantha`. `say -v '?'` lists 184 of them. Set `random` for a new voice each session |
| `SPEAK_VOICE_POOL` | engine's own pool | comma-separated names the random pick draws from |
| `SPEAK_RATE` | system rate | words per minute; the macOS default is about 175 |
| `SPEAK_MAX_CHARS` | `400` | truncation cap, at a word boundary |
| `NEMO_HTTP_URL` | `http://127.0.0.1:8082` | OpenAI-compatible speech server: `/v1/audio/speech`, and `/v1/audio/transcriptions` when listening |
| `NEMO_API_KEY` | — | bearer token for that server |
| `NEMO_TTS_MODEL` | `magpie` | model name sent in the request |
| `SPEAK_LISTEN` | — | set `1` for the `listen` tool and `/talk` |
| `NEMO_ASR_MODEL` | `nemotron-3.5` | transcription model the server loads and the request names |
| `SPEAK_LISTEN_SILENCE_MS` | `3000` | silence that ends an utterance |
| `SPEAK_LISTEN_MAX_SECONDS` | `240` | hard cap on one recording |
| `SPEAK_LISTEN_TIMEOUT` | `10` | seconds to wait for you to start talking |
| `SPEAK_LISTEN_LANGUAGE` | — | language hint for transcription, e.g. `en` |
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
against 0.7 s for `say`. Resident memory once the server answers `/health` is 780 MB
with the voice alone and 1.5 GB when `SPEAK_LISTEN` adds the transcription model.
Both grow in use, and not by the same amount: one spoken sentence added 120 MB to the
voice-only server but only 45 MB to the one carrying both models, and a single
transcription took that one to 2.4 GB. The first sentence after a cold start is spoken
by `say` while the server warms up, so speech is never delayed and never silently
dropped.

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

`/plugin install` **copies** this repo into `~/.claude/plugins/cache/` rather than
linking it, so editing the checkout changes nothing Claude Code loads. Run
`scripts/install-local.sh` to sync the working tree into that copy — it refuses to
write anywhere outside the plugin cache — then restart Claude Code.

303 tests, no network, no audio and no microphone: the queue runs against a fake
spawner, the NeMo engine and the transcriber against a mock HTTP transport, the
supervisor and the microphone lock against injected spawn, liveness and kill callables
over a temporary state directory, and the end-to-end tests drive the real server
process over stdio with the `command` engine pointed at a recorder script. Capture is
split so the decision to stop recording is a pure state machine fed block energies,
which is what lets every capture path be tested without audio hardware. No test starts
a speech server, downloads a model, or opens an input device.

## Layout

```
.claude-plugin/plugin.json       plugin manifest
.claude-plugin/marketplace.json  so the repo installs as a marketplace
.mcp.json                        registers the stdio server
scripts/speak-mcp                finds uv and launches the server
scripts/speak-talk               finds uv and runs one /talk capture
scripts/install-nemo.sh          idempotent bootstrap, run by /speak:setup
scripts/install-local.sh         sync this checkout into the plugin cache Claude Code loads
commands/setup.md                the /speak:setup command
commands/talk.md                 the /talk command, and how to read a transcript
harness/architecture.md          system shape and the decisions behind it
src/speak/__main__.py            entry point: environment to objects, then serve
src/speak/config.py              settings, parsed and validated from the environment
src/speak/server.py              the tools over the queue, the engine, and the ears
src/speak/tools.py               tool descriptions and the when-to-speak policy
src/speak/queue.py               one process at a time, non-blocking enqueue
src/speak/engines/               say · nemo · command, and their voice lists
src/speak/engines/asr.py         transcription against the shared speech server
src/speak/capture.py             the microphone, and when an utterance has ended
src/speak/miclock.py             one holder at a time for the one microphone
src/speak/talk.py                the /talk entry point: record, transcribe, print
src/speak/runtime.py             logging, the health probe, and capture limits
src/speak/supervisor.py          starts, shares, and stops the speech server
src/speak/text.py                whitespace collapsing and the length cap
```

## Notes

Speech is per session: the queue and the chosen voice live in the MCP server process
and die with the session, which is the behaviour you want. `stop_speaking` is the
escape hatch when a queued sentence is no longer wanted.

The speech server is the opposite — one per machine, not one per session, because it
costs the better part of a gigabyte, and half again as much once the transcription
model is loaded beside it. Sessions coordinate through `SPEAK_STATE_DIR`: a lock
directory elects whichever session starts it, and each session leaves a file behind so
the last one out can stop it. Sessions that crash are pruned by pid, so a lost window
cannot pin the memory forever.

The microphone is arbitrated the same way, under `mic/` in the same directory: the
directory is the lock and the file inside it names the holder, so a lock left by a
session that died is reclaimed by the next one to want it. The reclaim goes by pid,
so on the rare occasion the operating system has reused a dead holder's number you
are told "another session (pid N) is listening" about a process that has nothing to
do with this plugin. Delete `mic/` under `SPEAK_STATE_DIR` and try again.

One consequence of sharing: the transcription model is loaded only by the session that
happens to win the start election, and only if that session had `SPEAK_LISTEN` set. A
listening session that finds a server someone else started without it is told so —
`listen` cannot restart the server to fix itself without cutting off whichever window
is mid-sentence.
