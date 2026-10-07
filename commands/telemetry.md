---
description: Show or change voicebox's telemetry setting, print this install's id, or show the notice
argument-hint: "[on|local|off|--id|notice]"
allowed-tools: Bash(uv run:*)
---

Run `uv run --directory "${CLAUDE_PLUGIN_ROOT}" --quiet voicebox-telemetry $ARGUMENTS` and
show its output as it is. If `uv` is not on PATH, use `~/.local/bin/uv`.

With no argument it prints the current setting. `off` collects nothing and sends only an
anonymous install count; `local` keeps the data on this machine and sends only that
count; `on` is the default. `--id` prints the install id to quote in a deletion request.
`DO_NOT_TRACK=1` in the environment sends nothing at all. TELEMETRY.md in the plugin has
every field.
