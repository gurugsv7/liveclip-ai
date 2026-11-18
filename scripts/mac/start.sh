#!/usr/bin/env bash
# Start LiveClip AI: backend serves the built UI at http://127.0.0.1:8765 and the browser opens.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
for b in /opt/homebrew/bin /usr/local/bin; do case ":$PATH:" in *":$b:"*) ;; *) [ -d "$b" ] && PATH="$b:$PATH" ;; esac; done
export PATH
VPY="$ROOT/backend/.venv/bin/python"
[ -x "$VPY" ] || { echo "Run scripts/mac/setup.sh first"; exit 1; }
[ -f frontend/dist/index.html ] || ( cd frontend && npm run build )
PORT="${LIVECLIP_PORT:-8765}"
if curl -fs "http://127.0.0.1:$PORT/api/system/status" >/dev/null 2>&1; then
  echo "LiveClip AI is already running"; open "http://127.0.0.1:$PORT/"; exit 0
fi
mkdir -p data/logs
( cd backend && nohup "$VPY" -m liveclip --port "$PORT" > "$ROOT/data/logs/server.out" 2> "$ROOT/data/logs/server.err" & )
for _ in $(seq 1 40); do
  sleep 0.5
  curl -fs "http://127.0.0.1:$PORT/api/system/status" >/dev/null 2>&1 && break
done
open "http://127.0.0.1:$PORT/"
echo "LiveClip AI running at http://127.0.0.1:$PORT  (API docs: /docs). Stop with scripts/mac/stop.sh"
