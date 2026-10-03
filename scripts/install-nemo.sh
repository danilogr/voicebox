#!/usr/bin/env bash
# Install NeMo-Speech.cpp and the models the speak plugin uses.
# Idempotent: safe to run again. Downloads about 1.2 GB the first time.
set -euo pipefail

BIN="${SPEAK_NEMO_BIN:-nemo-speech}"
INSTALLER="${SPEAK_NEMO_INSTALLER:-https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh}"
# The supervisor runs the server on --device auto. Below 0.2.0, MagpieTTS on Metal
# returns DC saturation instead of speech on M5-class Macs, so older builds are upgraded.
MIN_VERSION=0.2.0

installed_version() {
  "$BIN" --version 2>/dev/null | awk '{print $2; exit}'
}

if command -v "$BIN" >/dev/null 2>&1; then
  have="$(installed_version || true)"
  if [ "$(printf '%s\n%s\n' "$MIN_VERSION" "$have" | sort -V | head -n 1)" = "$MIN_VERSION" ]; then
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

echo "pulling the MagpieTTS model (520 MB with its codec, skipped if present)"
"$BIN" pull magpie

echo "pulling the transcription model (710 MB, skipped if present)"
"$BIN" pull nemotron-3.5

echo
"$BIN" doctor
echo
echo "Done. Set SPEAK_ENGINE=nemo and the plugin will start the server on first use."
echo "Speaking and listening are both on from then; nothing else to switch."
