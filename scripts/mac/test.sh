#!/usr/bin/env bash
# Run all automated tests. --fast skips the ~2 min media integration suite.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
for b in /opt/homebrew/bin /usr/local/bin; do [ -d "$b" ] && PATH="$b:$PATH"; done
export PATH
VPY="$ROOT/backend/.venv/bin/python"
cd "$ROOT/backend"
if [ "${1:-}" = "--fast" ]; then "$VPY" -m pytest tests/test_units.py tests/test_recall.py -v; else "$VPY" -m pytest -v; fi
BE=$?
cd "$ROOT/frontend" && npx vitest run
FE=$?
[ $BE -eq 0 ] && [ $FE -eq 0 ]
