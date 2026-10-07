---
description: Install NVIDIA NeMo-Speech.cpp and the models voicebox needs to transcribe, write voice-overs and read aloud
---

# Set up voicebox

voicebox runs entirely on this machine, powered by NVIDIA NeMo-Speech.cpp
(https://github.com/NVIDIA/NeMo-Speech.cpp): MagpieTTS for speech and Nemotron 3.5 for
recognition. Explain the cost before you start: about 1.4 GB of models on disk, and while
the speech server runs, roughly 1.75 GB of resident memory.

1. Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet speak-core-setup` and show the
   output. It installs `nemo-speech` if it is missing or older than 0.2.0, pulls both
   models, and runs `nemo-speech doctor`. It is idempotent. If `uv` is not on PATH, use
   `~/.local/bin/uv`.
2. If it fails, report the failure verbatim rather than working around it. A partial
   install is worse than none.
3. Check `command -v ffmpeg ffprobe`. Transcription needs both, and so do m4a and mp3
   voice-overs. If either is missing, say so and suggest `brew install ffmpeg` on macOS.
4. On success, say what is now available: `transcribe` for audio and video files,
   `synthesize` for turning a text or a file into one audio file, and `/voicebox:narrate`
   for reading a long text aloud. The plugin starts and stops the speech server on its own.
5. Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet voicebox-telemetry notice` and
   show its output word for word: it is voicebox's one-time telemetry notice.

Do not start the server yourself and do not edit the user's shell profile without asking.
