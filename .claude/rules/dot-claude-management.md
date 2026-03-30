# .claude/ File Management

**NEVER use mkdir, Write, Edit, or Bash to create or modify files under `.claude/` in this repo or the supervised repo.**

Instead, use the `dot-claude-*` and `researcher-dot-claude-*` pixi tasks:

## For this repo (self)
- `pixi run dot-claude-list` — list all .claude/ assets (not yet available — use direct Read for now)

## For the supervised repo (researcher)
- `pixi run researcher-dot-claude-list` — list researcher's .claude/ assets
- `pixi run researcher-dot-claude-read <path>` — read an asset
- `pixi run researcher-dot-claude-edit <path> --sed 's/old/new/g'` — targeted find/replace (logged, diffed, auto-committed)
- `echo "content" | pixi run researcher-dot-claude-edit <path>` — full content replacement (logged, diffed, auto-committed)
- `echo "content" | pixi run researcher-dot-claude-diff <path>` — preview diff without writing

## Prefer --sed for targeted edits
When changing specific strings in an asset, use `--sed` instead of piping the entire file:
```bash
pixi run researcher-dot-claude-edit skill --sed 's|old-pattern|new-pattern|g'
```
This is more precise than reading the file, transforming with external `sed`, and piping back.

## Creating new skills in the researcher
```bash
echo "skill content" | pixi run researcher-dot-claude-edit skills/my-skill/SKILL.md
```

## Why
Every .claude/ change must be logged, diffed, and auto-committed via `harness_core.prompt_editor`. Direct file operations (including sed, awk, and other shell tools) bypass this tracking. This applies to ALL subagents — never dispatch an agent that uses Write or Edit on .claude/ files.
