---
description: Show everything the speak plugin's telemetry has collected on this machine
---

Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet speak-stats` and show its output
as it stands. If `uv` is not on PATH, use `~/.local/bin/uv`.

It reads the local telemetry database and changes nothing. Do not summarise away rows
the user may want to see; add a short summary after the output only if they ask.
