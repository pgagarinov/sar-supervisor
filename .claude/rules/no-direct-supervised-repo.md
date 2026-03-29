# Rule: Never run commands directly in the supervised repo

**NEVER** run any command with cwd set to the supervised repo. This includes:
- `cd <supervised-repo> && <anything>`
- `pytest`, `python`, `git status`, `git diff`, `ls`, or any other command
- Reading files via the `Read` tool (except `.claude/` assets via `prompt-read`)

The outer researcher does NOT interact with the supervised repo directly. That is the inner worker's job.

Instead, use the harness CLI from the workspace root:

```bash
# Start/stop the inner worker
pixi run researcher-loop
pixi run researcher-stop

# Monitor progress
pixi run researcher-status
pixi run researcher-monitor
pixi run researcher-watch-status

# Capture state (includes reports, git status, code-state)
pixi run researcher-snapshot

# View history
pixi run researcher-history

# Edit .claude prompt assets
/edit-prompts   # or: pixi run researcher-dot-claude-read/researcher-dot-claude-edit/researcher-dot-claude-diff

# Code state management
pixi run researcher-revert-safe
pixi run researcher-restore best
pixi run researcher-restore <snapshot-id>
```

If you need report data, read it from snapshots or the report paths configured in `harness.toml` — never run commands in the supervised repo yourself.
