---
name: edit-prompts
description: "Read, edit, and diff .claude prompt assets (SKILL.md, agent definitions) in the supervised repo."
user_invocable: true
arguments:
  - name: action
    description: "Action: list, read <name>, edit <name>, diff <name>, history"
    required: true
---

# /edit-prompts — Prompt Asset Editor

This skill is the **only** way to read or modify `.claude/` files in the supervised repo. Direct `Read`/`Edit`/`Write` calls to files under the supervised repo's `.claude/` directory are forbidden — use this skill instead.

## Why

Every prompt asset change must be:
1. **Logged** — recorded in `.supervisor/prompt-edits.jsonl` with timestamp, sha1, and diff stats
2. **Diffed** — the full unified diff is shown before and after edits
3. **Traceable** — so the researcher can correlate prompt changes with metric changes across runs

## Available Assets

Use `pixi run researcher-dot-claude-list` to discover available assets. The asset names are derived from `harness.toml` configuration (skill name + agent names).

You can also use paths relative to `.claude/` (e.g., `rules/some-rule.md`).

## Actions

### List all assets
```bash
pixi run researcher-dot-claude-list
pixi run researcher-dot-claude-list --json
```

### Read an asset
```bash
pixi run researcher-dot-claude-read skill
pixi run researcher-dot-claude-read <agent-name>
```
Or use the CLI directly and read the output.

### Edit an asset
To edit, you must:
1. Read the current content with `researcher-dot-claude-read`
2. Prepare the new content
3. Pipe it to `researcher-dot-claude-edit`:
```bash
echo "new content" | pixi run researcher-dot-claude-edit skill
```

In practice as Claude Code, the workflow is:
1. `Bash("eval \"$(pixi shell-hook)\" && PYTHONPATH=src python -m supervisor_harness.cli prompt-read skill")` — read current content
2. Make your changes to the content
3. `Bash("eval \"$(pixi shell-hook)\" && PYTHONPATH=src python -m supervisor_harness.cli prompt-edit skill <<'PROMPT_EOF'\n<new content>\nPROMPT_EOF")` — write and get diff

### Diff without writing
```bash
echo "proposed content" | pixi run researcher-dot-claude-diff skill
```

### View edit history
```bash
pixi run researcher-dot-claude-history
pixi run researcher-dot-claude-history --json
```

## Workflow for Prompt Changes

1. **Always snapshot first**: `pixi run researcher-snapshot --label before-edit`
2. **Read** the asset you want to change
3. **Edit** with the new content — review the diff output
4. **Snapshot after**: `pixi run researcher-snapshot --label after-edit`
5. **Restart** the loop: `pixi run researcher-loop`
