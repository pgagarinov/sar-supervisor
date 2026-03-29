#!/bin/bash
# Run a single experiment with a specific SKILL.md variant.
#
# Usage: ./experiments/run_experiment.sh <variant-file> [budget_minutes] [--experiment-id ID] [--base-branch BRANCH]
#
# Example:
#   ./experiments/run_experiment.sh experiments/variants/EXAMPLE-variant.md 20
#   ./experiments/run_experiment.sh experiments/variants/EXAMPLE-variant.md 20 --experiment-id exp-001
#   ./experiments/run_experiment.sh experiments/variants/EXAMPLE-variant.md 20 --base-branch exp-000
#
# This script:
# 1. Applies the variant SKILL.md via prompt-edit
# 2. Starts the inner loop (with experiment_id if provided)
# 3. Monitors for budget_minutes (default: 20)
# 4. Stops the inner loop
# 5. Captures a snapshot tagged with the variant name
# 6. Reports the final metric value

set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT_FILE="${1:?Usage: run_experiment.sh <variant-file> [budget_minutes] [--experiment-id ID] [--base-branch BRANCH]}"
BUDGET_MINUTES="${2:-20}"
shift 2 || shift $#

# Parse optional flags
EXPERIMENT_ID=""
BASE_BRANCH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment-id)
            EXPERIMENT_ID="$2"
            shift 2
            ;;
        --base-branch)
            BASE_BRANCH="$2"
            shift 2
            ;;
        *)
            echo "Unknown flag: $1" >&2
            exit 1
            ;;
    esac
done

VARIANT_NAME="$(basename "$VARIANT_FILE" .md)"

echo "=== Experiment: $VARIANT_NAME ==="
echo "    Variant file: $VARIANT_FILE"
echo "    Budget: ${BUDGET_MINUTES} minutes"
[ -n "$EXPERIMENT_ID" ] && echo "    Experiment ID: $EXPERIMENT_ID"
[ -n "$BASE_BRANCH" ] && echo "    Base branch: $BASE_BRANCH"
echo ""

# 1. Apply the variant SKILL.md
echo "--- Applying variant SKILL.md ---"
cat "$VARIANT_FILE" | pixi run researcher-dot-claude-edit skill
echo ""

# 2. Clean temp files
pixi run clean
echo ""

# 3. Start the inner loop
echo "--- Starting inner loop ---"
EXP_ARGS=""
[ -n "$EXPERIMENT_ID" ] && EXP_ARGS="$EXP_ARGS --id $EXPERIMENT_ID"
[ -n "$BASE_BRANCH" ] && EXP_ARGS="$EXP_ARGS --base-branch $BASE_BRANCH"

if [ -n "$EXP_ARGS" ]; then
    pixi run researcher-experiment start --no-clean $EXP_ARGS &
    LOOP_PID=$!
else
    pixi run researcher-start --no-clean &
    LOOP_PID=$!
fi
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
    if ! pixi run researcher-status 2>&1 | grep -q "running: True"; then
        echo "    Inner loop stopped on its own at ${ELAPSED}s"
        break
    fi

    echo "    ${ELAPSED}s elapsed, ~${REMAINING}m remaining"
done

# 5. Stop the inner loop
echo ""
echo "--- Stopping inner loop ---"
if [ -n "$EXPERIMENT_ID" ]; then
    pixi run researcher-experiment stop --id "$EXPERIMENT_ID" || true
else
    pixi run researcher-stop || true
fi

# 6. Capture snapshot
echo "--- Capturing snapshot ---"
SNAPSHOT=$(pixi run researcher-snapshot --label "exp-${VARIANT_NAME}")
echo "    Snapshot: $SNAPSHOT"

# 7. Report results
echo ""
echo "=== Results: $VARIANT_NAME ==="
echo "    Check: pixi run researcher-history --limit 1"
echo ""
echo "Done. Snapshot at: $SNAPSHOT"
