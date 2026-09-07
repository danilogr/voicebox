#!/usr/bin/env bash
# Sync this working tree into the plugin directory Claude Code actually loads.
#
# /plugin install copies the repo into the cache rather than linking it, so a
# branch checkout here is invisible until the copy is refreshed. Run this after
# checking out a branch, then restart Claude Code.
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
manifest="$repo/.claude-plugin/plugin.json"

version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -1)
if [ -z "$version" ]; then
  echo "could not read a version from $manifest" >&2
  exit 1
fi

target="$HOME/.claude/plugins/cache/speak-marketplace/speak/$version"

# --delete is destructive, so refuse anything outside the plugin cache.
case "$target" in
  "$HOME/.claude/plugins/cache/"*) ;;
  *)
    echo "refusing to sync into $target: outside the plugin cache" >&2
    exit 1
    ;;
esac

if [ ! -d "$target" ]; then
  echo "no install found at $target" >&2
  echo "run /plugin install speak@speak-marketplace once, then re-run this." >&2
  exit 1
fi

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.mypy_cache/' \
  "$repo"/ "$target"/

echo "synced $repo -> $target"
echo "restart Claude Code to load it."
