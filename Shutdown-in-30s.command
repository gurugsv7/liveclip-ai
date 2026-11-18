#!/usr/bin/env bash
# macOS: double-click to stop LiveClip AI cleanly and shut the Mac down in 30 seconds.
# Cancel within 30 s:  pkill -f liveclip-shutdown-timer
cd "$(dirname "$0")" && bash scripts/mac/shutdown-30s.sh
read -r -p "Press Enter to close this window..."
