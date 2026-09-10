---
description: Speak your next prompt instead of typing it
allowed-tools: Bash(sh:*)
---

The text at the bottom of this message is a speech-to-text transcript of what the
user just said out loud. It was produced by a small model running on this machine
and may contain errors: misheard words, wrong homophones, missing punctuation,
names spelled the way they sound.

Treat it as the user's request and act on it. Where a word looks garbled, prefer
the reading that makes sense for this repository. Before doing anything
destructive or irreversible - deleting files, force-pushing, dropping data,
running a migration - read the transcript back and wait for a yes.

If the transcript is "(heard nothing)", tell the user you heard nothing and stop.
A line beginning `speak:` is an error from the capture script, not the user's
words: report that line as it stands and stop. It is printed on both streams, so
it reaches you whichever one this message carries.

---

!`sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak-talk"`
