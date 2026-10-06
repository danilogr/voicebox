---
description: Install NeMo-Speech.cpp and the models the speak plugin needs for a neural voice and for voice input
---

# Set up neural speech for the speak plugin

Install the local speech server this plugin talks to, NVIDIA NeMo-Speech.cpp. Explain the cost before
you start: about 1.4 GB of models on disk, and while the server runs, roughly 830 MB
of resident memory for the voice alone or 1.75 GB with the transcription model beside
it.

1. Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet speak-core-setup` and show
   the output. It installs NVIDIA NeMo-Speech.cpp (https://github.com/NVIDIA/NeMo-Speech.cpp)
   as `nemo-speech` if it is missing or older than 0.2.0, pulls the MagpieTTS voice and
   the nemotron-3.5 transcription model, and runs `nemo-speech doctor`. It is
   idempotent. It pulls both models, because the plugin both speaks and listens. If
   `uv` is not on PATH, use `~/.local/bin/uv`.
2. If the script fails, report the failure verbatim rather than working around
   it. A partial install is worse than none.
3. On success, tell the user to set `SPEAK_ENGINE=nemo` in their environment,
   and that the plugin starts and stops the server on its own from then on.
4. Voice input needs nothing else switched on. Mention that the speech server
   starts when the session opens rather than on first use, so `/talk` does not
   pay a cold start, and that this costs memory from session start.
5. On macOS the first recording asks for microphone access for the app running
   Claude Code — the terminal, not Claude Code itself. The switch is in System
   Settings > Privacy & Security > Microphone. Warn that a denied microphone opens
   the stream and delivers silence rather than failing, so it reads the same as a
   muted input: the plugin reports that no microphone is delivering audio and lists
   the devices it tried, with permission among the causes to check. Say that "heard
   nothing" means something different — a live microphone whose gate never opened,
   so either nothing was said or the recording ran out first.
6. Mention that the first sentence after a cold start is spoken by the macOS
   `say` voice while the server warms up (about 7 seconds), and every sentence
   after that uses the neural voice.
7. Offer to list the available voices with the `list_voices` tool and to set one
   with `set_voice`.

Do not start the server yourself and do not edit the user's shell profile
without asking.
