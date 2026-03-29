# Rule: Never revert the supervised repo directly

**NEVER** run `git checkout -- .`, `git checkout -- <path>`, `git clean -fd`, or any destructive git command directly in the supervised repo.

Accumulated production code changes represent hours of worker effort. A raw revert permanently destroys them.

Instead, use the safe commands that auto-checkpoint before reverting:

```bash
# Safe revert (checkpoints first, then reverts configured paths)
pixi run researcher-revert-safe

# Full revert (checkpoints first, then reverts entire working tree)
pixi run researcher-revert-safe --full

# Restore a previous best state
pixi run researcher-restore best

# Restore a specific snapshot
pixi run researcher-restore <snapshot-id>
```

Every snapshot captures a `code-state/` directory with:
- `tracked.patch` — git diff of all tracked file modifications
- `untracked.tar.gz` — archive of all untracked files

This means any snapshot can be fully restored later.
