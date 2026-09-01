---
description: Install NeMo-Speech.cpp and the MagpieTTS model so the speak plugin can use a neural voice
---

# Set up neural speech for the speak plugin

Install the local speech server this plugin can talk to. Explain the cost before
you start: about 1.2 GB of models on disk and roughly 1 GB of memory whenever the
server is running.

1. Run `${CLAUDE_PLUGIN_ROOT}/scripts/install-nemo.sh` and show the output. It
   installs `nemo-speech` if it is missing, pulls the MagpieTTS model, and runs
   `nemo-speech doctor`. It is idempotent.
2. If the script fails, report the failure verbatim rather than working around
   it. A partial install is worse than none.
3. On success, tell the user to set `SPEAK_ENGINE=nemo` in their environment,
   and that the plugin starts and stops the server on its own from then on.
4. Mention that the first sentence after a cold start is spoken by the macOS
   `say` voice while the server warms up (about 10 seconds), and every sentence
   after that uses the neural voice.
5. Offer to list the available voices with the `list_voices` tool and to set one
   with `set_voice`.

Do not start the server yourself and do not edit the user's shell profile
without asking.
