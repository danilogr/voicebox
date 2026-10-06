# Speak for Claude Code

Give Claude Code a voice and an ear.

When Claude finishes a long task, gets blocked, or needs a decision, it speaks a short sentence aloud so you can step away from your terminal without losing your place. When you want to reply, you can speak your prompt instead of typing it.

---

## What You Can Do

- **Spoken Alerts**: Claude speaks a brief update when a task completes or when it needs your input.
- **Voice Dictation (`/talk`)**: Speak your next prompt out loud. The local speech model transcribes your words directly into your message.
- **Full Spoken Conversations (`/speak:converse`)**: Turn your entire session into a continuous conversation. Claude speaks its responses and automatically listens for your answers.
- **Article & Document Narration (`/speak:narrate`)**: Have Claude read a web article, markdown document, or long explanation aloud while you listen.
- **Local Audio & Video Transcription (`transcribe`)**: Transcribe local media files (`.mp3`, `.mp4`, `.mov`, etc.) into text, word timings, and subtitle files (`.srt`, `.vtt`).

---

## Quick Start

### 1. Install the Plugin

In your Claude Code terminal:

```bash
/plugin marketplace add ~/dev/claude-plugins/speak
/plugin install speak@speak-marketplace
```

Restart Claude Code. The plugin automatically sets up its environment using [uv](https://docs.astral.sh/uv/) and works immediately with your Mac's built-in system voice.

### 2. Optional: Set Up Neural Voices and Voice Input

To use natural neural voices and enable voice input (`/talk` and `listen`), run:

```bash
/speak:setup
```

This downloads a local speech and transcription server ([NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp), ~1.4 GB). Once downloaded, the plugin manages the server in the background for you.

---

## Core Features & How to Use Them

### Spoken Alerts
By default, Claude uses the `speak` tool only when necessary:
- When a task is complete.
- When it needs clarification or confirmation.
- When an error requires your attention.

Claude is instructed never to narrate routine progress or read code diffs aloud.

### Dictating Prompts (`/talk`)
Instead of typing your prompt:
1. Run `/talk`.
2. A chime plays to signal that the microphone is open.
3. Speak your request. When you stop talking for 3 seconds, a closing chime plays.
4. Your spoken words are transcribed and inserted as your next prompt.

### Conversation Mode (`/speak:converse`)
Run `/speak:converse` to switch the session into interactive voice mode. In this mode, Claude speaks its replies and immediately listens for your spoken answer whenever it asks a question.

To return to standard text mode at any time, simply tell Claude to stop or ask for quiet.

### Narrating Long Texts (`/speak:narrate`)
Have Claude read a document or URL out loud:

```bash
/speak:narrate ./docs/spec.md
/speak:narrate https://example.com/article
```

Narration reads the author's prose clearly while skipping code blocks, tables, and visual references. If you interrupt with `stop_speaking`, it stops immediately without losing your place.

### File Transcription (`transcribe`)
Ask Claude to transcribe any local audio or video file:

> *"Transcribe meeting.mp4 into subtitles"*

Claude calls the `transcribe` tool to run the transcription as a background task. You can check progress anytime with `transcription_status`.

---

## Available Tools

Claude uses these tools automatically during your session:

| Tool | Description |
|---|---|
| `speak(text)` | Queues a sentence to be spoken. Utterances play in order and never overlap. |
| `stop_speaking()` | Immediately silences current playback and clears any queued sentences. |
| `listen(max_seconds?)` | Opens the microphone, records your reply, and returns the transcript. |
| `list_voices()` | Lists available voices for the active speech engine. |
| `set_voice(voice)` | Changes the voice for the current session. |
| `speech_status()` | Reports playback status and the number of utterances in the queue. |
| `transcribe(path)` | Starts a background transcription job for a local audio/video file. |
| `transcription_status(job_id?)` | Checks the status, progress, or results of a transcription job. |

---

## Available Commands

| Command | What it does |
|---|---|
| `/talk` | Record and transcribe your next prompt through the microphone. |
| `/speak:converse` | Start two-way spoken conversation mode for the session. |
| `/speak:narrate` | Read a file, webpage, or conversation text out loud. |
| `/speak:setup` | Install local NeMo models for neural voices and voice input. |

---

## Configuration

You can customize voices, engines, and audio thresholds via environment variables or in `.mcp.json`:

| Setting | Default | Description |
|---|---|---|
| `SPEAK_ENGINE` | `say` | Speech engine: `say` (macOS native), `nemo` (neural local server), or `command` (custom CLI). |
| `SPEAK_VOICE` | system voice | Active voice name. Set to `random` to pick a new voice each session. |
| `SPEAK_VOICE_POOL` | default pool | Comma-separated voice list used when `SPEAK_VOICE=random`. |
| `SPEAK_RATE` | system rate | Playback speed in words per minute (macOS default is ~175). |
| `SPEAK_MAX_CHARS` | `400` | Maximum character cap per spoken sentence. |
| `SPEAK_INPUT_DEVICE` | system default | Pin a specific microphone by name. |
| `SPEAK_LISTEN_SILENCE_MS` | `3000` | Milliseconds of silence before recording automatically stops. |
| `SPEAK_LISTEN_MAX_SECONDS` | `240` | Maximum recording length in seconds. |
| `SPEAK_LISTEN_CUE` | `1` | Set to `0` to mute start and stop microphone chimes. |

### Speech Engines

- **`say`** (default on macOS): Zero-setup, instant playback using Apple's built-in synthesizer.
- **`nemo`**: High-fidelity neural voice (MagpieTTS) and streaming speech recognition (Nemotron-3.5). Runs locally with Metal/CUDA acceleration.
- **`command`**: Route synthesis through an external command (such as Piper or espeak-ng):
  ```bash
  SPEAK_ENGINE=command SPEAK_COMMAND="piper --model en_US.onnx"
  ```

---

## Privacy & Telemetry

- **Audio stays local**: All speech synthesis, microphone recordings, and file transcriptions run 100% on your machine. No audio or transcript text is ever sent over the network.
- **Anonymous metrics**: By default, basic counts and timings (never text or audio) are shared to improve engine support. See [`TELEMETRY.md`](TELEMETRY.md) for full details.
- **Turn off telemetry**: Run `speak-telemetry off` or set `DO_NOT_TRACK=1` to disable reporting completely.

---

## Developer Guide

For developers contributing to `speak`:

```bash
uv sync                  # Install dependencies
uv run pytest            # Run the test suite (770+ offline tests)
uv run ruff check .      # Lint checks
uv run mypy              # Type checks
scripts/install-local.sh # Sync changes into Claude Code's plugin cache
```

---

## Documentation

- **[Codebase Layout](harness/layout.md)**: File-by-file map of all modules and scripts.
- **[Agents Operating Rules](AGENTS.md)**: Operating rules and standards for AI coding assistants.
- **[Privacy & Telemetry Specification](TELEMETRY.md)**: Detailed disclosure of telemetry fields and opt-out commands.
- **[Voice Tuning Guide](voice-tuning/README.md)**: Guide to recording a personal dataset and fine-tuning recognition.
- **[Architecture](harness/architecture.md)**: System design, component boundaries, and design decisions.
- **[User Flow & State Machines](harness/user-flow.md)**: Playback lifecycle, silence tracking, and input selection.
- **[Performance & Benchmarks](harness/performance.md)**: Latency, resident memory, and disk footprint measurements.
- **[Integration Guide](harness/integration.md)**: How to integrate the local speech stack into other agents or scripts.
- **[Design Guidelines](harness/design-guidelines.md)**: Development conventions, error handling, and testing practices.
