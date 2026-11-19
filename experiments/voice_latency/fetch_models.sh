#!/usr/bin/env bash
# Fetch the three sherpa-onnx release models used by measure.py (github.com releases; worked from the
# sandbox on 2026-11-19 while huggingface.co was blocked). ~310 MB download, ~455 MB extracted.
set -euo pipefail
DEST="${ISLAB_MODELS:-models}"
DL="$(dirname "$DEST")/dl"
mkdir -p "$DEST" "$DL"
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download
for u in asr-models/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17.tar.bz2 \
         asr-models/sherpa-onnx-whisper-tiny.en.tar.bz2 \
         tts-models/vits-piper-en_US-amy-low.tar.bz2; do
  f="$DL/$(basename "$u")"
  [ -s "$f" ] || curl -sSL -o "$f" "$BASE/$u"
  tar xjf "$f" --no-same-owner -C "$DEST"
done
echo "models in $DEST"
