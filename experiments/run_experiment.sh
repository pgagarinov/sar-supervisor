#!/bin/bash
# Run a single experiment with a specific SKILL.md variant.
#
# Usage: ./experiments/run_experiment.sh <variant-file> [budget_minutes]
#
# Example:
#   ./experiments/run_experiment.sh experiments/variants/EXAMPLE-variant.md 20
#
# This script:
# 1. Applies the variant SKILL.md via prompt-edit
# 2. Starts the inner loop
# 3. Monitors for budget_minutes (default: 20)
# 4. Stops the inner loop
# 5. Captures a snapshot tagged with the variant name
# 6. Reports the final metric value

set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT_FILE="${1:?Usage: run_experiment.sh <variant-file> [budget_minutes]}"
BUDGET_MINUTES="${2:-20}"
VARIANT_NAME="$(basename "$VARIANT_FILE" .md)"

echo "=== Experiment: $VARIANT_NAME ==="
echo "    Variant file: $VARIANT_FILE"
echo "    Budget: ${BUDGET_MINUTES} minutes"
echo ""

# 1. Apply the variant SKILL.md
echo "--- Applying variant SKILL.md ---"
cat "$VARIANT_FILE" | pixi run prompt-edit skill
echo ""

# 2. Clean temp files
pixi run clean
echo ""

# 3. Start the inner loop
echo "--- Starting inner loop ---"
pixi run start --no-clean &
LOOP_PID=$!
sleep 10

# 4. Wait for budget
echo "--- Running for ${BUDGET_MINUTES} minutes ---"
SECONDS_BUDGET=$((BUDGET_MINUTES * 60))
ELAPSED=0
POLL_INTERVAL=30

while [ $ELAPSED -lt $SECONDS_BUDGET ]; do
    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
    REMAINING=$(( (SECONDS_BUDGET - ELAPSED) / 60 ))

    # Check if still running
    if ! pixi run status 2>&1 | grep -q "running: True"; then
        echo "    Inner loop stopped on its own at ${ELAPSED}s"
        break
    fi

    echo "    ${ELAPSED}s elapsed, ~${REMAINING}m remaining"
done

# 5. Stop the inner loop
echo ""
echo "--- Stopping inner loop ---"
pixi run stop || true

# 6. Capture snapshot
echo "--- Capturing snapshot ---"
SNAPSHOT=$(pixi run snapshot --label "exp-${VARIANT_NAME}")
echo "    Snapshot: $SNAPSHOT"

# 7. Report results
echo ""
echo "=== Results: $VARIANT_NAME ==="
echo "    Check: pixi run history --limit 1"
echo ""
echo "Done. Snapshot at: $SNAPSHOT"
