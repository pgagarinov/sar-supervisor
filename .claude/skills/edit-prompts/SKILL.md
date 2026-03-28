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

Use `pixi run prompt-list` to discover available assets. The asset names are derived from `harness.toml` configuration (skill name + agent names).

You can also use paths relative to `.claude/` (e.g., `rules/some-rule.md`).

## Actions

### List all assets
```bash
pixi run prompt-list
pixi run prompt-list --json
```

### Read an asset
```bash
pixi run prompt-read skill
pixi run prompt-read <agent-name>
```
Or use the CLI directly and read the output.

### Edit an asset
To edit, you must:
1. Read the current content with `prompt-read`
2. Prepare the new content
3. Pipe it to `prompt-edit`:
```bash
echo "new content" | pixi run prompt-edit skill
```

In practice as Claude Code, the workflow is:
1. `Bash("eval \"$(pixi shell-hook)\" && PYTHONPATH=src python -m supervisor_harness.cli prompt-read skill")` — read current content
2. Make your changes to the content
3. `Bash("eval \"$(pixi shell-hook)\" && PYTHONPATH=src python -m supervisor_harness.cli prompt-edit skill <<'PROMPT_EOF'\n<new content>\nPROMPT_EOF")` — write and get diff

### Diff without writing
```bash
echo "proposed content" | pixi run prompt-diff skill
```

### View edit history
```bash
pixi run prompt-history
pixi run prompt-history --json
```

## Workflow for Prompt Changes

1. **Always snapshot first**: `pixi run snapshot --label before-edit`
2. **Read** the asset you want to change
3. **Edit** with the new content — review the diff output
4. **Snapshot after**: `pixi run snapshot --label after-edit`
5. **Restart** the loop: `pixi run loop`
