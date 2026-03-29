# Rule: All .claude edits in the supervised repo go through /edit-prompts

**NEVER** use `Read`, `Edit`, or `Write` tools directly on files under the supervised repo's `.claude/` directory. This includes:
- `.claude/skills/*/SKILL.md`
- `.claude/agents/*.md`
- `.claude/rules/*.md`

Instead, use the `/edit-prompts` skill which:
- Logs every change to `.supervisor/prompt-edits.jsonl`
- Shows a unified diff of what changed
- Tracks sha1 hashes before and after

The CLI commands are:
- `dot-claude-list` — list all assets (use this to discover available names)
- `dot-claude-read <name>` — read an asset
- `dot-claude-edit <name>` — edit an asset (new content on stdin)
- `dot-claude-diff <name>` — diff proposed content without writing
- `dot-claude-history` — view edit log
