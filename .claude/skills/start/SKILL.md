---
name: start
description: "Start the research loop and begin monitoring"
user_invocable: true
---

# /start — Launch and Supervise the Research Loop

Start the inner research loop and begin active supervision. You are the outer researcher — your job is to make the inner researcher better at researching.

## SINGLE EXPERIMENT MODE (default)

### 1. Pre-flight

```bash
pixi run researcher-status
```
If already running, report state and stop.

Verify the supervised repo is accessible:
```bash
pixi run researcher-dot-claude-list
```
PASS if it lists the expected skill and agents.

### 2. Start the loop

```bash
pixi run researcher-loop --no-clean
```

This blocks while monitoring the researcher. The stop hook fires every ~120s with analysis. Act on the guidance: CONTINUE / INVESTIGATE / PIVOT.

### 3. Active supervision

On each stop hook cycle:
- Read the metric trend — is it improving, stalled, or regressing?
- Read deviation reports — is the researcher following its own protocol?
- Make a decision and ACT (see CLAUDE.md for the mandatory thinking protocol)

If stalled:
1. Read the researcher's current prompts: `pixi run researcher-dot-claude-read skill`, `pixi run researcher-dot-claude-read evaluator`, `pixi run researcher-dot-claude-read improver`
2. Analyze what the researcher is doing wrong (bad experiment discipline, not pivoting, repeating failed approaches, etc.)
3. Edit prompts: `echo "new content" | pixi run researcher-dot-claude-edit <name>`
4. Restart: `pixi run researcher-stop && pixi run researcher-loop --no-clean`

### 4. When the loop returns

Analyze results:
- `pixi run researcher-history` — metric progression
- `pixi run researcher-dot-claude-history` — what prompt changes were made

Decide: restart with same prompts, edit prompts and restart, or report success.

## PARALLEL EXPERIMENT MODE

Use when testing multiple researcher SKILL.md variants simultaneously.

### Setup

1. Create variants in `experiments/variants/` (each is a complete SKILL.md for the researcher)
2. For each variant:
   ```bash
   cat experiments/variants/X.md | pixi run researcher-dot-claude-edit skill
   pixi run researcher-experiment start --id exp-X
   ```
3. Monitor all: `pixi run researcher-experiment list`
4. Compare: `pixi run researcher-experiment compare`
5. Select winner, stop losers: `pixi run researcher-experiment stop --id exp-X`
6. Continue best variant as the main run

## What You Do NOT Do

- **Never interact with the target directly** — you don't know what the target is. The researcher handles that.
- **Never read target code, run target commands, or verify target state** — that's the researcher's domain.
- **Never edit target files** — your lever is the researcher's prompt assets only.
- You supervise the **researcher's methodology**, not its domain work.
