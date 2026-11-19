#!/usr/bin/env bash
# LiveClip AI - macOS setup (Apple Silicon or Intel).
# Installs (only if missing) Homebrew packages: python@3.11, ffmpeg, node; then the backend
# virtualenv (FastAPI, Streamlink, faster-whisper, OpenCV), the frontend build, the Whisper
# and YAMNet models, and the test fixtures.
#
#   bash scripts/mac/setup.sh            (from the project folder)
#   bash scripts/mac/setup.sh --no-model (skip pre-downloading the Whisper model)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
NO_MODEL=0; WHISPER_MODEL="small"
for a in "$@"; do
  case "$a" in
    --no-model) NO_MODEL=1 ;;
    --whisper-model=*) WHISPER_MODEL="${a#*=}" ;;
  esac
done
step() { printf '\n\033[35m==> %s\033[0m\n' "$1"; }

step "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh  (install it, then run this script again)"; exit 1
fi

step "Python 3.11, FFmpeg, Node.js"
command -v python3.11 >/dev/null 2>&1 || brew install python@3.11
command -v ffmpeg     >/dev/null 2>&1 || brew install ffmpeg
command -v node       >/dev/null 2>&1 || brew install node
ffmpeg -hide_banner -version | head -1
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox; then
  echo "Apple VideoToolbox encoder available (hardware exports)"
else
  echo "VideoToolbox not found in this FFmpeg: exports will use x264 (CPU)"
fi
ffmpeg -hide_banner -filters 2>/dev/null | grep -q " subtitles " || echo "WARNING: this FFmpeg has no libass; captions need it (brew install ffmpeg includes it)"

step "Backend virtual environment"
PY="$(command -v python3.11 || echo /opt/homebrew/bin/python3.11)"
[ -x backend/.venv/bin/python ] || "$PY" -m venv backend/.venv
VPY="$ROOT/backend/.venv/bin/python"
"$VPY" -m pip install --upgrade pip wheel
"$VPY" -m pip install -e "backend[ai,vision,test]"
"$VPY" -m pip install --upgrade streamlink yt-dlp   # sites change; always the newest
"$ROOT/backend/.venv/bin/streamlink" --version

step "Frontend"
( cd frontend && npm install --no-audit --no-fund && npm run build )

step "Configuration"
[ -f .env ] || { cp .env.example .env; echo "Created .env (edit it to set paths/keys)"; }

if [ "$NO_MODEL" = "0" ]; then
  step "Whisper model ($WHISPER_MODEL)"
  mkdir -p data/models
  "$VPY" -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER_MODEL', device='cpu', compute_type='int8', download_root='data/models'); print('model ready')"
fi

step "Sound-event model (YAMNet, Apache-2.0, 16 MB)"
mkdir -p data/models/yamnet
[ -f data/models/yamnet/yamnet.onnx ] || curl -fsSL -o data/models/yamnet/yamnet.onnx "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet.onnx"
[ -f data/models/yamnet/yamnet_class_map.csv ] || curl -fsSL -o data/models/yamnet/yamnet_class_map.csv "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet_class_map.csv"

step "Test fixtures"
"$VPY" -m liveclip.fixtures

chmod +x scripts/mac/*.sh ./*.command 2>/dev/null || true
step "Done"
echo "Start the app:  bash scripts/mac/start.sh   (or double-click LiveClip.command)"
echo "Run tests:      bash scripts/mac/test.sh"
