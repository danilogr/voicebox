---
description: Show what voicebox has recorded about your own jobs on this machine
allowed-tools: Bash(uv run:*)
---

Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet voicebox-stats` and show its output
as it is. If `uv` is not on PATH, use `~/.local/bin/uv`. Do not summarise it unless asked.
