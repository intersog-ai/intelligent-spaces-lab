#!/usr/bin/env bash
# B05 extra test voices: two more Piper low voices from the sherpa-onnx GitHub releases (~67 MB each).
# Run after experiments/voice_latency/fetch_models.sh (B01), with the same ISLAB_MODELS.
set -euo pipefail
DEST="${ISLAB_MODELS:-models}"
DL="$(dirname "$DEST")/dl"
mkdir -p "$DEST" "$DL"
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models
for v in vits-piper-en_US-ryan-low vits-piper-en_GB-alan-low; do
  f="$DL/$v.tar.bz2"
  [ -s "$f" ] || curl -sSL -o "$f" "$BASE/$v.tar.bz2"
  tar xjf "$f" --no-same-owner -C "$DEST"
done
echo "voices in $DEST"
