#!/usr/bin/env bash
# Download an extra Whisper model once, e.g. the bigger one used for Tamil clip captions.
# Run it while you're NOT monitoring streams: large-v3 is ~3 GB.
# Usage: bash scripts/mac/download-model.sh [large-v3]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODEL="${1:-large-v3}"
echo "Downloading Whisper $MODEL to $ROOT/data/models (one time)..."
"$ROOT/backend/.venv/bin/python" -c "from faster_whisper.utils import download_model; print(download_model('$MODEL', cache_dir='$ROOT/data/models'))"
echo "Done. Restart LiveClip to use it."
