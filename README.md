# <img src="assets/favicon.png" width="36" height="36" alt="Walkie logo" valign="middle" /> Walkie

Give Claude Code a voice, an ear, and a **private local transcription workstation**.

**Walkie** (formerly `speak`) gives your terminal agent audio superpowers:
1. **Speaks & Listens**: Claude speaks a short sentence aloud when a long task completes or when it needs input, and lets you speak prompts back instead of typing.
2. **On-Device Audio & Video Transcription**: Transcribe meetings, interviews, voice memos, and videos entirely on your machine using a compact, local speech model—with zero cloud fees, complete data privacy, speaker labeling, and subtitle exports.

---

## What You Can Do

- **Local Audio & Video Transcription**: Drop any media file (`.mp3`, `.m4a`, `.mp4`, `.mov`, etc.) into your workspace and get speaker-labeled text, word timestamps, or subtitle files (`.srt`, `.vtt`) in the background.
- **Hands-Free Task Alerts**: Claude speaks a brief status update when a long build or test finishes, or when it hits a blocker.
- **Voice Dictation (`/talk`)**: Speak your next prompt through your microphone instead of typing.
- **Interactive Spoken Conversations (`/speak:converse`)**: Turn your entire session into a two-way spoken dialogue.
- **Document & Article Narration (`/speak:narrate`)**: Have Claude read a web page, markdown file, or PR description aloud while you work.

---

## Local Audio & Video Transcription

If you regularly transcribe meetings, user interviews, lectures, podcasts, or video clips, `speak` gives you a dedicated local transcription workflow directly inside Claude Code.

### Why Transcribe Locally?

- **100% Private & On-Device**: Audio never leaves your computer. Transcribe sensitive customer calls, confidential meetings, and proprietary demos without sending data to external APIs or cloud providers.
- **Free & Unlimited**: No subscriptions, no usage tiers, and no per-minute API bills.
- **Runs in the Background**: Transcription runs as a detached background worker. You can continue writing code, running tests, or asking Claude questions while your file transcribes.
- **Fast, Compact Local Model**: Uses NVIDIA's streaming `nemotron-3.5` model (0.6B parameters) running locally via NeMo-Speech.cpp with hardware acceleration (Metal on Apple Silicon). It processes roughly 7 minutes of audio per minute of work.
- **Speaker Labeling (Diarization)**: Automatically identifies and separates different speakers in meetings or multi-person interviews.
- **Multiple Output Formats**:
  - **Plain Text**: Clean reading format for meeting recaps, notes, or documentation.
  - **JSON**: Detailed word-level timestamps and speaker IDs for indexing and search.
  - **Subtitles (`.srt`, `.vtt`)**: Ready to use in video editing software or web video players.
- **Automatic Language Detection & Translation**: Transcribe speech in dozens of languages or translate foreign-language audio directly to English.
- **Universal Format Support**: Any media format supported by `ffmpeg` works out of the box (`.mp3`, `.m4a`, `.wav`, `.aac`, `.flac`, `.mp4`, `.mov`, `.mkv`).

### Example Prompts for Claude

You can trigger transcriptions naturally during any Claude session:

> *"Transcribe `meeting.m4a` with speaker labels and write a summary of key decisions."*
>
> *"Generate `.srt` subtitles for `demo.mp4`."*
>
> *"Transcribe `interview.mov` to JSON with word timestamps."*
>
> *"Transcribe `french-lecture.mp3` and translate it to English."*
>
> *"What is the status of my transcription job?"*

Claude starts the job in the background, reports the estimated completion time, and lets you check on progress anytime with `transcription_status`.

---

## Quick Start

### 1. Install the Plugin

In your Claude Code terminal:

```bash
/plugin marketplace add ~/dev/claude-plugins/speak
/plugin install speak@speak-marketplace
```

Restart Claude Code. The plugin automatically prepares its environment using [uv](https://docs.astral.sh/uv/) and works immediately with your Mac's built-in system voice.

### 2. Set Up the Local Speech & Transcription Model

For file transcription and neural voice features, run the one-time setup:

```bash
/speak:setup
```

This installs NeMo-Speech.cpp and downloads the compact local models (~1.4 GB total: MagpieTTS for speech synthesis and Nemotron-3.5 for streaming speech recognition). The plugin manages the local server process automatically.

*Note: File transcription requires `ffmpeg` (`brew install ffmpeg`).*

---

## Conversational Voice Features

### Dictating Prompts (`/talk`)
Say your prompt instead of typing:
1. Run `/talk`.
2. A chime confirms the microphone is open.
3. Speak your request. When you finish (3 seconds of silence), a closing chime sounds.
4. Your spoken words are transcribed by the local model and inserted directly as your next message.

### Interactive Conversation Mode (`/speak:converse`)
Run `/speak:converse` to turn the session into a continuous voice conversation. Claude speaks its responses aloud and automatically listens for your voice reply whenever it needs input. Tell Claude to stop or ask for quiet to return to text mode at any time.

### Reading Long Documents Aloud (`/speak:narrate`)
Have Claude read any local file or web article:

```bash
/speak:narrate ./docs/spec.md
/speak:narrate https://example.com/blog-post
```

Narration reads prose verbatim while filtering out code blocks, tables, and visual links, playing in small 10-sentence rounds that stop instantly if interrupted.

---

## Available Tools

Claude uses these tools automatically based on your instructions:

| Tool | Description |
|---|---|
| `transcribe(path, format?, diarize?, language?, translate?)` | Starts a background transcription job for a local audio or video file. |
| `transcription_status(job_id?)` | Checks status, progress, completion preview, or failure reasons for transcription jobs. |
| `speak(text)` | Queues a sentence to be spoken out loud. Plays in order without overlapping. |
| `stop_speaking()` | Instantly silences current playback and clears pending sentences. |
| `listen(max_seconds?)` | Opens the microphone, records your answer, and returns the transcript. |
| `list_voices()` | Lists available voices for the active speech engine. |
| `set_voice(voice)` | Changes the voice for the current session. |
| `speech_status()` | Reports playback status and the number of utterances in the queue. |

---

## Available Commands

| Command | What it does |
|---|---|
| `/talk` | Speak your next prompt through the microphone instead of typing. |
| `/speak:converse` | Start two-way spoken conversation mode for the current session. |
| `/speak:narrate` | Read a document, web page, or conversation snippet out loud. |
| `/speak:setup` | Install local NeMo models for transcription and neural voices. |

---

## Configuration

Customize settings via environment variables or in `.mcp.json`:

| Setting | Default | Description |
|---|---|---|
| `SPEAK_TRANSCRIBE_DIR` | input file dir | Destination folder for generated transcript files. |
| `SPEAK_ENGINE` | `say` | Speech output engine: `say` (macOS native), `nemo` (neural local server), or `command`. |
| `SPEAK_VOICE` | system voice | Active voice name (or `random` for a new voice each session). |
| `SPEAK_VOICE_POOL` | default pool | Comma-separated pool of voices used when `SPEAK_VOICE=random`. |
| `SPEAK_RATE` | system rate | Playback speed in words per minute (macOS default is ~175). |
| `SPEAK_MAX_CHARS` | `400` | Maximum character length cap per spoken sentence. |
| `SPEAK_INPUT_DEVICE` | system default | Pin a specific microphone input by name. |
| `SPEAK_LISTEN_SILENCE_MS` | `3000` | Milliseconds of silence before recording automatically stops. |
| `SPEAK_LISTEN_MAX_SECONDS` | `240` | Maximum recording duration in seconds. |
| `SPEAK_LISTEN_CUE` | `1` | Set to `0` to mute start and stop microphone chimes. |

### Speech Engines

- **`say`** (default on macOS): Instant playback with zero setup using Apple's built-in speech synthesizer.
- **`nemo`**: High-quality neural voice (MagpieTTS) and streaming speech recognition (Nemotron-3.5). Runs locally with Metal/CUDA acceleration.
- **`command`**: Route speech output through a custom command line (Piper, espeak-ng, etc.):
  ```bash
  SPEAK_ENGINE=command SPEAK_COMMAND="piper --model en_US.onnx"
  ```

---

## Privacy & Telemetry

- **100% On-Device Audio**: All speech synthesis, microphone recordings, and file transcriptions run entirely on your hardware. Audio, transcripts, and file names never leave your machine.
- **Anonymous Metrics**: By default, anonymous event counts and execution timings are shared to track engine stability. No text, audio, file paths, or personal identifiers are ever collected. See [`TELEMETRY.md`](TELEMETRY.md) for complete details.
- **Opt-Out**: Run `speak-telemetry off` or set `DO_NOT_TRACK=1` to disable reporting completely.

---

## Developer Guide

```bash
uv sync                  # Install dependencies
uv run pytest            # Run the test suite (770+ offline tests)
uv run ruff check .      # Lint checks
uv run mypy              # Type checks
scripts/install-local.sh # Sync changes into Claude Code's plugin cache
```

---

## Documentation Index

- **[Codebase Layout](harness/layout.md)**: File-by-file map of all modules and scripts.
- **[Agents Operating Rules](AGENTS.md)**: Operating rules and standards for AI coding assistants.
- **[Privacy & Telemetry Specification](TELEMETRY.md)**: Detailed disclosure of telemetry fields and opt-out commands.
- **[Voice Tuning Guide](voice-tuning/README.md)**: Guide to recording a personal dataset and fine-tuning recognition.
- **[Architecture](harness/architecture.md)**: System design, component boundaries, and design decisions.
- **[User Flow & State Machines](harness/user-flow.md)**: Playback lifecycle, silence tracking, and input selection.
- **[Performance & Benchmarks](harness/performance.md)**: Latency, resident memory, and disk footprint measurements.
- **[Integration Guide](harness/integration.md)**: How to integrate the local speech stack into other agents or scripts.
- **[Design Guidelines](harness/design-guidelines.md)**: Development conventions, error handling, and testing practices.
