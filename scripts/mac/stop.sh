#!/usr/bin/env bash
# Graceful shutdown: finalizes open candidates, stops capture workers, flushes the DB.
# Force-stops after 20 s if the backend doesn't exit.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${LIVECLIP_PORT:-8765}"
if curl -fs -X POST -H "Origin: http://127.0.0.1:$PORT" "http://127.0.0.1:$PORT/api/system/shutdown" >/dev/null 2>&1; then
  echo "Shutdown requested"
else
  echo "Backend not responding"
fi
PIDF="$ROOT/data/liveclip.pid"
for _ in $(seq 1 40); do [ -f "$PIDF" ] || break; sleep 0.5; done
if [ -f "$PIDF" ]; then
  P="$(cat "$PIDF")"
  echo "Forcing stop of PID $P"
  kill -TERM "$P" 2>/dev/null; sleep 2; kill -KILL "$P" 2>/dev/null
  rm -f "$PIDF"
fi
# orphaned capture processes started by LiveClip (their command line points at our data folder)
pkill -f "ffmpeg.*$ROOT/data" 2>/dev/null || true
pkill -f "streamlink.*--stdout" 2>/dev/null || true
echo "Stopped."
