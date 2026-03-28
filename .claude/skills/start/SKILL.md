---
name: start
description: "Start the research loop and begin monitoring"
user_invocable: true
---

# /start — Launch the Research Loop

Start the inner research loop and begin the supervisor monitoring cycle.

## Steps

1. **Check if already running**:
   ```bash
   pixi run status
   ```
   If running, report the current state and stop.

2. **Verify the research loop repo is accessible**:
   Check that the supervised repo path from `harness.toml` exists and has `.claude/skills/`.

3. **Verify the RAG target is accessible**:
   Check that `../sar-rag-target` (relative to the research loop) exists and `pixi run eval` works.

4. **Start the loop** (preserving any existing code changes):
   ```bash
   pixi run loop --no-clean
   ```
   This launches `claude -p /improve-rag` in the research loop repo and begins monitoring.

5. **Report**: PID, log path, and the current metric value.

## What Happens Next

The supervisor loop:
- Monitors the stream-json log
- Snapshots on state changes
- Fires the stop hook periodically (every 120s)
- The stop hook analyzes metric trends and detects deviations
- If stalled, the outer researcher (YOU) should read snapshots, analyze why, and edit the research loop's SKILL.md or agent definitions via `/edit-prompts`
