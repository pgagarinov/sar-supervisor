---
name: clean
description: "Clean supervisor state, logs, and temp files"
user_invocable: true
---

# /clean — Clean Supervisor State

Remove all supervisor runtime state so the next `/start` begins fresh.

## Steps

1. Stop the inner loop if running:
   ```bash
   pixi run stop
   ```

2. Clean supervisor state:
   ```bash
   pixi run clean --include-log --include-snapshots
   ```

3. Clean temp report files (paths from harness.toml `[reports]` section):
   ```bash
   pixi run clean --include-log
   ```
   This removes all report files, log files, and PID/state files configured in harness.toml.

4. Report what was cleaned.
