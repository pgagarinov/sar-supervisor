---
name: stop
description: "Stop the research loop and capture final snapshot"
user_invocable: true
---

# /stop — Stop the Research Loop

Gracefully stop the running research loop and capture the final state.

## Steps

1. **Check current status**:
   ```bash
   pixi run status
   ```
   If not running, report and stop.

2. **Capture a pre-stop snapshot**:
   ```bash
   pixi run snapshot --label pre-stop
   ```

3. **Stop the process**:
   ```bash
   pixi run stop
   ```

4. **Capture final snapshot**:
   ```bash
   pixi run snapshot --label final
   ```

5. **Report final state**:
   - Last metric value from history
   - Number of snapshots captured
   - Research loop's results.tsv if it exists (in the research loop repo)
   - Summary: how many iterations ran, best metric achieved

## After Stopping

To restart with a modified approach:
1. Use `/edit-prompts` to modify the research loop's SKILL.md or agent definitions
2. Use `/start-research` to restart

To revert the RAG target's code:
```bash
pixi run revert-safe
```

To restore a previous best state:
```bash
pixi run restore best
```
