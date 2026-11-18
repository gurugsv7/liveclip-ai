#!/usr/bin/env bash
# Stop LiveClip AI cleanly, then shut the Mac down in 30 seconds.
# Cancel within the 30 s:  pkill -f liveclip-shutdown-timer
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "Stopping LiveClip AI (finishing open clips, closing recordings)..."
bash "$ROOT/scripts/mac/stop.sh"
# $0 of the inner shell is "liveclip-shutdown-timer", so `pkill -f liveclip-shutdown-timer` cancels it
nohup bash -c 'sleep 30 && osascript -e "tell application \"System Events\" to shut down"' liveclip-shutdown-timer >/dev/null 2>&1 &
echo "Mac shuts down in 30 s. To cancel run:  pkill -f liveclip-shutdown-timer"
echo "(macOS may ask once to allow Terminal to control System Events.)"
