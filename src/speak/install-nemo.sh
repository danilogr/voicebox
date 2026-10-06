#!/usr/bin/env bash
# Install NeMo-Speech.cpp and the models every speak-core plugin uses.
# Idempotent: safe to run again. Downloads about 1.4 GB the first time.
set -euo pipefail

BIN="${NEMO_BIN:-nemo-speech}"
INSTALLER="${NEMO_INSTALLER:-https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh}"
# The supervisor runs the server on --device auto. Below 0.2.0, MagpieTTS on Metal
# returns DC saturation instead of speech on M5-class Macs, so older builds are upgraded.
MIN_VERSION=0.2.0

installed_version() {
  "$BIN" --version 2>/dev/null | awk '{print $2; exit}'
}

at_least_min() {
  [ "$(printf '%s\n%s\n' "$MIN_VERSION" "$1" | sort -V | head -n 1)" = "$MIN_VERSION" ]
}

if command -v "$BIN" >/dev/null 2>&1; then
  have="$(installed_version || true)"
  if at_least_min "$have"; then
    echo "nemo-speech $have already installed at $(command -v "$BIN")"
  else
    echo "nemo-speech ${have:-of unknown version} is older than $MIN_VERSION; upgrading from $INSTALLER"
    curl -fsSL "$INSTALLER" | sh
  fi
else
  echo "installing NeMo-Speech.cpp from $INSTALLER"
  curl -fsSL "$INSTALLER" | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# The upstream installer writes to its own prefix, so an older copy earlier on PATH
# (or an absolute NEMO_BIN) survives an upgrade and is what the plugin would run.
have="$(installed_version || true)"
if ! at_least_min "$have"; then
  echo "nemo-speech at $(command -v "$BIN" || echo "$BIN") is ${have:-of unknown version}," \
    "still older than $MIN_VERSION." >&2
  echo "Remove that copy or put ~/.local/bin first on PATH, then run this again." >&2
  exit 1
fi

echo "pulling the MagpieTTS model (640 MB with its codec, skipped if present)"
"$BIN" pull magpie

echo "pulling the transcription model (710 MB, skipped if present)"
"$BIN" pull nemotron-3.5

echo
"$BIN" doctor
echo
echo "Done. NeMo-Speech.cpp and both models are installed; the plugins start and stop"
echo "the speech server on their own."
