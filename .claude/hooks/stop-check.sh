#!/usr/bin/env bash
# Stop hook: structured decision support for the research loop
set -euo pipefail

# Detect workspace root from script location
WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"

# Read sleep duration from harness.toml if available, default to 120
SLEEP_SECONDS=120
if command -v python3 &>/dev/null && [ -f "$WORKSPACE/harness.toml" ]; then
    SLEEP_SECONDS=$(python3 -c "
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print(120); sys.exit()
with open('$WORKSPACE/harness.toml', 'rb') as f:
    c = tomllib.load(f)
print(c.get('stop_hook', {}).get('sleep_seconds', 120))
" 2>/dev/null || echo 120)
fi

# Wait before checking — gives the inner loop time to make progress
# Keep well under the 210s hook timeout to avoid silent failures
sleep "$SLEEP_SECONDS"

cd "$WORKSPACE"
eval "$(pixi shell-hook)"
PYTHONPATH=src python -m supervisor_harness.stop_hook
