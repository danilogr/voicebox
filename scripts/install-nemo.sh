#!/usr/bin/env bash
# Install NeMo-Speech.cpp and the MagpieTTS model for the speak plugin.
# Idempotent: safe to run again. Downloads about 1.2 GB the first time.
set -euo pipefail

BIN="${SPEAK_NEMO_BIN:-nemo-speech}"
INSTALLER="${SPEAK_NEMO_INSTALLER:-https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh}"

if command -v "$BIN" >/dev/null 2>&1; then
  echo "nemo-speech already installed at $(command -v "$BIN")"
else
  echo "installing NeMo-Speech.cpp from $INSTALLER"
  curl -fsSL "$INSTALLER" | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "pulling the MagpieTTS model (about 500 MB, skipped if present)"
"$BIN" pull magpie

echo
"$BIN" doctor
echo
echo "Done. Set SPEAK_ENGINE=nemo and the plugin will start the server on first use."
