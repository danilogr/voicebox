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
MagpieTTS and the transcription model — 1.2 GB together — then the plugin manages the
server for you.

## Tools

| Tool | What it does |
|---|---|
| `speak(text, voice?, rate?)` | Queues one sentence and returns immediately. Utterances play in order and never overlap. |
| `stop_speaking()` | Silences what is playing and drops everything queued behind it. |
| `list_voices()` | The voices the current engine can use, and which one is active. |
| `set_voice(voice)` | Changes the voice for the rest of this session. |
| `speech_status()` | Reports how many utterances are waiting, being prepared, prepared, or playing, and how many have finished. |
| `listen(max_seconds?)` | Records you through the microphone and returns what you said. Waits for anything queued to finish playing first — `/talk`, in its own process, cannot. |

`listen` only exists when `SPEAK_LISTEN=1`; the other five are always there.

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

Set it *before* Claude Code starts. The plugin reads its environment once, when the
session opens, so exporting it in your shell profile has no effect on a session that
is already running — you need a new one, started from a shell that has the variable.
Getting this wrong is quiet: `listen` is simply not among the tools and `/talk`
refuses. `claude mcp list` shows the plugin either way; what tells you is whether
`listen` is there.

Two ways in:

- **`/speak:converse`** — turn the whole session into a spoken conversation: Claude
  says its answers aloud and calls `listen` when it needs an answer from you, rather
  than ending the turn and waiting for typing. It is one instruction file and no code,
  so it lasts for the session and stops when you ask it to.
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

A chime says the microphone is open, and a lower one says it has closed. Both ends
matter: with `/talk` you press enter and otherwise have no way to tell whether to
start talking now or in two seconds, and every recording stops on silence rather than
on a keypress, so without the second chime you never learn when it decided you had
finished. The opening chime is played to completion *before* the microphone opens —
the gate calibrates on the first 300 ms of audio, and a sound still playing then
would be averaged into it. Turn the pair off with `SPEAK_LISTEN_CUE=0`, or replace
them with `SPEAK_LISTEN_CUE_START` and `SPEAK_LISTEN_CUE_END`.

The cue sounds ship with the plugin as 16-bit PCM WAV, synthesised by
`scripts/make-cues.py` rather than borrowed from the operating system — a system sound
lives at a different path on every platform, and carrying someone else's audio is not
ours to do. Any WAV works as an override.
A chime that cannot be played is logged and skipped — it never costs you a recording.

A capture that caught nothing closes with an error tone instead of the usual chime
(overridable with `SPEAK_LISTEN_CUE_EMPTY`). Recording stops on silence
either way, so without a different sound an empty capture is indistinguishable from
one that worked until the transcript fails to arrive.

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
`/talk` runs as its own short-lived process and would otherwise pay a cold start
every time. The trade is that the memory is held from session start, and a server
carrying both models holds about twice what a voice-only one does — figures in
[harness/performance.md](harness/performance.md).

### Which microphone

Your system default input is tried first, so the device you pick in System Settings
is the one that gets used. If it delivers nothing, the other inputs are tried in
turn and the first live one is kept for the rest of the session — until it too goes
quiet, at which point the search starts again from the default, so a microphone you
fix mid-session is picked straight up.

"Delivers nothing" means exact digital silence, and only that. A live microphone
always carries a noise floor, so a stream of pure zeros is a muted input rather
than a quiet room — a fact, not a guess, which is why a device is never skipped
merely for being quiet. An input whose level slider sits at zero, a built-in
microphone disabled because the lid is shut, and the virtual devices Teams and Zoom
install are indistinguishable from here, and all of them read as muted.

A device gets 1.5 seconds to produce its first sample before it is called mute:
CoreAudio hands over exact zeros while an input primes, and a Bluetooth microphone
can take most of a second to start. Calibration runs from that first real sample
rather than from the stream opening, so priming silence stays out of the ambient
average — a gate diluted by zeros lands at the floor, and in a noisy room a
floor-level gate opens on the room and never closes.

Nothing is remembered between sessions and nothing is written to disk, so a fresh
session always looks at your current default first. Name one device with
`SPEAK_INPUT_DEVICE` and it is used without a search — and if it is mute you are
told so, rather than quietly recorded from somewhere else.

When every input returns silence, you get that as the error, with the devices it
tried. Check System Settings > Sound > Input and the level slider there, and that
the app running Claude Code — the terminal, not Claude Code itself — is allowed the
microphone under Privacy & Security. macOS does not fail a denied microphone; it
hands over silence, which now reads as a muted device rather than as your silence.

"heard nothing" is left to mean only what it says: a live microphone that never
heard the gate open. A `max_seconds` shorter than the time you took to start
talking ends the same way. The log line for every capture names the device, the
gate it calibrated, and the loudest block it saw, which is usually enough to tell
those apart.

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
| `SPEAK_LISTEN_CUE` | `1` | set `0` to open and close the microphone silently |
| `SPEAK_LISTEN_CUE_START` | shipped `cue-open.wav` | chime as the microphone opens |
| `SPEAK_LISTEN_CUE_END` | shipped `cue-close.wav` | chime as it closes |
| `SPEAK_LISTEN_CUE_EMPTY` | shipped `cue-empty.wav` | tone when nothing was captured |
| `SPEAK_INPUT_DEVICE` | — | record from this input only, instead of searching |
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

It is slower per sentence than `say` and takes a few seconds to start, so the first
sentence after a cold start is spoken by `say` while the server warms up: speech is
never delayed waiting for a model and never silently dropped. The supervisor always
passes `--device cpu`, because Metal runs this workload slower on Apple silicon.
Latency, memory and disk figures are in
[harness/performance.md](harness/performance.md).

**`command`** — hands the sentence to any binary as its last argument. Useful for
piper, espeak, a shell script, or a remote speaker:

```bash
SPEAK_ENGINE=command SPEAK_COMMAND="/usr/local/bin/piper --model en_US.onnx"
```

### Other platforms

Voice input is portable: the microphone goes through PortAudio, and the cue sounds ship
with the plugin as WAV. Playback needs a player, found on the machine at startup —
`afplay` on macOS, PowerShell's `SoundPlayer` on Windows, and on Linux the first of
`paplay`, `aplay`, `ffplay`, `play` or `mpv` that is installed. With none of them, cues
are skipped and a recording still works.

Speech output is the part that is not portable by default: `SPEAK_ENGINE=say` is macOS
only, and on any other platform the plugin says so once at startup. Use
`SPEAK_ENGINE=nemo` for the local speech server, which is cross-platform, or
`SPEAK_ENGINE=command` with whatever that machine has — `espeak-ng` and `spd-say` both
take the sentence as their last argument.

None of this reaches a session whose process runs in the cloud. There is no speaker or
microphone attached to a remote container, and `/talk` injects a local command's output
into your prompt, which has no meaning in a remote runtime. Install it wherever Claude
Code runs on hardware you own.

### From another MCP client

The server is a standard stdio MCP server, so any MCP client — another editor, a desktop
app, an agent built on the MCP SDK — can launch it and get the same tools. Point the
client at the checkout rather than `${CLAUDE_PLUGIN_ROOT}`, which only Claude Code
expands, and pass settings through the client's own environment block, which also means
nothing has to be exported before the client starts:

```json
{
  "mcpServers": {
    "speak": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/speak", "--quiet", "speak-mcp"],
      "env": { "SPEAK_ENGINE": "nemo", "SPEAK_LISTEN": "1" }
    }
  }
}
```

With voice input on, the server also offers conversation mode as an MCP prompt named
`converse`, served from the same file as `/speak:converse`, for clients that surface
prompts. `/talk` has no equivalent outside Claude Code: it records before your message
exists, through a slash command that runs a local script.

`listen` holds its tool call open until you stop talking. A client that times out long
tool calls will cut it off mid-recording, so in those, ask for a short `max_seconds`.

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

412 tests, no network, no audio and no microphone: the queue and the recording cues
run against a fake spawner, the NeMo engine and the transcriber against a mock HTTP
transport, the supervisor and the microphone lock against injected spawn, liveness and
kill callables over a temporary state directory, and the end-to-end tests drive the real server
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
harness/user-flow.md             every path, state and early exit, end to end
harness/performance.md           measured latency, memory, disk, and audio levels
commands/converse.md             conversation mode: /speak:converse, and the converse MCP prompt
scripts/make-cues.py             regenerates the three shipped cue sounds
src/speak/assets/                the cue sounds, synthesised and shipped as WAV
src/speak/player.py              finds something on this machine that plays a WAV
src/speak/__main__.py            entry point: environment to objects, then serve
src/speak/config.py              settings, parsed and validated from the environment
src/speak/server.py              the tools over the queue, the engine, and the ears
src/speak/tools.py               tool descriptions and the when-to-speak policy
src/speak/queue.py               one player at a time, the next prepared while it plays
src/speak/engines/               say · nemo · command, and their voice lists
src/speak/engines/asr.py         transcription against the shared speech server
src/speak/capture.py             the microphone, and when an utterance has ended
src/speak/cue.py                 the chimes that bracket one recording
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
