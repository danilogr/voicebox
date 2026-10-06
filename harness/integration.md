# Integrating the speech stack

> For building voice into an agent that is **not** this plugin — a local model, a
> custom harness, a script. What to call, what to run yourself, and what will bite
> you. The plugin's own internals live in `architecture.md`; measured figures in
> `performance.md`.
>
> Everything here runs on the machine. Nothing in this guide sends audio, text, or
> telemetry anywhere.

## Three ways in

| | What you get | What you write | Use it when |
|---|---|---|---|
| **HTTP API** | Speech and transcription over OpenAI-compatible routes | Playback, mic capture, turn-taking | You are writing the agent loop yourself — **start here** |
| **MCP server** | The plugin's tools, policy included | Nothing, if your harness is an MCP client | Your harness already speaks MCP and your model calls tools reliably |
| **Python modules** | Playback, mic capture with silence detection, a queue, a cross-process mic lock | Glue | You want the fiddly local-audio parts without rewriting them |

They compose: most custom agents want the HTTP API for the models and a couple of
modules for the microphone.

## The HTTP API

The models are served by
[NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) (`nemo-speech serve`),
not by this plugin. The plugin is one client of it.

### Run your own instance

Do not attach to the plugin's server. It belongs to the plugin's supervisor, which
stops it when the last Claude Code session using it exits — your agent would lose
its voice when an unrelated editor window closed. Start a second one on another
port:

```bash
nemo-speech serve --port 8083 --device auto --no-ui \
  --asr-model nemotron-3.5 --tts-model magpie --tts.seed 6285
```

- `--device` takes `auto`, `cpu`, `cuda[:N]`, `metal`, or `vulkan[:N]`. The plugin
  runs `auto` (Metal on Apple silicon) and needs NeMo-Speech.cpp 0.2.0 or later. On
  0.2.0, CPU synthesis on an M5 is about 0.07x realtime, too slow for speech.
- `--tts.seed` pins voice timbre. **Without it the same voice name sounds different
  from one server start to the next** — surprising enough that it cost this project
  an afternoon.
- `--api-key KEY` requires `Authorization: Bearer KEY` if you expose it beyond
  localhost. `--host` defaults to `127.0.0.1`; keep it there unless you mean it.

`nemo-speech serve --help` lists the rest.

### What is loaded

```bash
curl -sS http://127.0.0.1:8083/v1/models
```

Returns one entry per capability: a `transcription` model (id like
`.nemotron-3.5-asr-streaming-0.6b.q8_0.gguf`) and a `speech` model (`magpietts`),
the speech entry carrying its voices — John, Sofia, Aria, Jason, Leo — and its
languages: `en-US`, `es-ES`, `de-DE`, `fr-FR`, `it-IT`, `vi-VN`, `hi-IN`.

### Speech

```bash
curl -X POST http://127.0.0.1:8083/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"magpietts","voice":"Sofia","input":"your text"}' \
  -o out.wav
```

Returns a WAV. A 49-character sentence produced 2.6 seconds of audio in one
request.

### Transcription

```bash
curl -X POST http://127.0.0.1:8083/v1/audio/transcriptions \
  -F "file=@clip.wav"
```

Returns `{"text": "..."}`. The `model` field is optional; the loaded transcription
model is used either way, and both its full id and the short `nemotron-3.5` are
accepted. A 5.9-second 16 kHz mono clip came back in **0.5–0.7 s** on CPU, warm; on
Metal with 0.2.0, a 4-second clip came back in **0.06 s**.

16 kHz mono PCM WAV is the native input. The `transcribe` CLI resamples other rates
internally — a 48 kHz stereo file transcribes correctly with no conversion — and
the server shares that engine, but if you feed it something exotic, check the
transcript before trusting it.

### Both routes are OpenAI-compatible

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8083/v1", api_key="unused")

client.audio.speech.create(model="magpietts", voice="Sofia", input=text)
client.audio.transcriptions.create(model="nemotron-3.5", file=open(path, "rb"))
```

### Streaming

`serve --help` advertises realtime ASR over a WebSocket at `/v1/realtime`, which is
the natural fit for an always-listening agent. **Untested here** — this project has
never used it, so treat the shape as unverified and read the server's own docs.

### Long files

For a recording rather than an utterance, skip the HTTP API and run the CLI, which
is what this plugin's `transcribe` tool does:

```bash
nemo-speech --quiet transcribe in.wav --model nemotron-3.5 \
  -f srt -o out.srt --force --diarize
```

`-f` takes `text`, `json` (word timings and speakers), `srt`, `vtt`. Measured
throughput on an M5, CPU, including model load: **9.5x realtime**, **6.9x with
`--diarize`**. Budget roughly a minute of work per seven minutes of audio.

## Playback and capture are yours

The HTTP API returns and accepts audio files. Playing them, opening the microphone,
and deciding when the user stopped talking are the caller's job. Rather than
rewriting those, import them:

| Module | What it gives you |
|---|---|
| `speak_core.player` | `resolve_player(...)` picks a working playback command per platform from what is installed (`afplay`, `ffplay`, …) |
| `speak.capture` | `MicrophoneRecorder.record(TrackerSettings)` records one utterance and returns a `Recording` carrying the audio, why it stopped, and whether anyone actually spoke. `SilenceTracker` is a pure state machine — it calibrates against ambient noise instead of a fixed threshold, so a quiet mic and a noisy room both work |
| `speak_core.queue` | `SpeechQueue` plays one utterance at a time, in order, never overlapping |
| `speak.miclock` / `speak_core.exclusive` | One microphone holder at a time across processes, elected by an atomic `mkdir` |

Defaults worth knowing: capture is **16 kHz mono**, and an utterance ends after
**3 s** of silence.

speak-core is not published to PyPI — depend on it by git tag
(`speak-core @ git+https://github.com/danilogr/speak-core@v0.1.0`, a private
repository), or copy the files you need. They are plain `asyncio` with no MCP in them.

## The MCP server

If your harness is an MCP client, the plugin is a stdio server:

```bash
uv run --directory /path/to/speak python -m speak
```

Tools: `speak`, `listen`, `transcribe`, `transcription_status`, `list_voices`,
`set_voice`, `stop_speaking`, `speech_status`. Configuration is environment
variables — see the README's table.

Two caveats before choosing this. The tool descriptions carry policy written for
Claude Code (when to speak, when to stay quiet, one-line recaps), which your model
will also follow. And `listen` and `transcribe` assume a human at a desk: `listen`
waits for the speech queue to drain before opening the mic, and only one
transcription runs at a time, refused rather than queued.

## What will bite you

**The server pages out.** After the laptop slept, the process sat at 14 MB
resident; warm it is 778 MB. The first request after a wake or a long idle pays to
page the model back — one request took five minutes. Warm the server at startup
with a throwaway call, and do not put a tight timeout on the first request.

**One accelerator, one job.** A synthesis and a transcription at the same time
contend. This plugin serializes with a queue and a lock; give your agent an
equivalent, even if it is one mutex. A local LLM on the GPU contends with speech
too, but moving speech to CPU is not the way out on 0.2.0: CPU synthesis there is
slower than realtime.

**Paths must be absolute.** Anything spawned from a plugin or a launcher inherits
that launcher's working directory, not the user's project.

**Telemetry is the plugin's, not the server's.** Calling `nemo-speech` directly
sends nothing anywhere. The plugin's own collection is documented in
`TELEMETRY.md`. `SPEAK_TELEMETRY=off` disables it apart from the anonymous install
ping, and `DO_NOT_TRACK=1` disables both.

## Where things are

| Path | Responsibility |
|---|---|
| [`speak_core/engines/asr.py`](https://github.com/danilogr/speak-core/blob/main/src/speak_core/engines/asr.py) | speak_core's ASR client: how it calls `/v1/audio/transcriptions`, including liveness handling |
| [`speak_core/engines/`](https://github.com/danilogr/speak-core/blob/main/src/speak_core/engines/) | The speech engines, including the HTTP one |
| [`speak_core/supervisor.py`](https://github.com/danilogr/speak-core/blob/main/src/speak_core/supervisor.py) | Starting, sharing and stopping one server across processes |
| [`src/speak/transcribe/`](../src/speak/transcribe/) | The file-transcription feature: probe, convert, job records, the detached worker |
| [`src/speak/capture.py`](../src/speak/capture.py), [`speak_core/player.py`](https://github.com/danilogr/speak-core/blob/main/src/speak_core/player.py), [`speak_core/queue.py`](https://github.com/danilogr/speak-core/blob/main/src/speak_core/queue.py) | The reusable local-audio parts |
| [`harness/layout.md`](layout.md) | Exhaustive directory and file layout of the entire codebase |
| [`harness/performance.md`](performance.md) | Every measured number and the hardware behind it |
