---
name: my-skill
description: "Example variant — describe your strategy hypothesis here"
user_invocable: true
arguments:
  - name: zone
    description: "Optional scope limiter for the skill"
    required: false
---

# /my-skill — Variant Name

## ORCHESTRATOR DISCIPLINE

The orchestrator (main agent) is a DISPATCHER, not a worker.
Your ONLY job is to:
1. Launch agents
2. Read their JSON reports to decide what to dispatch next
3. Forward data verbatim between agents
4. Repeat until the metric reaches the target

You MUST NOT:
- Read source code directly
- Edit any file
- Analyze, diagnose, or summarize anything
- Use TodoWrite

## Agent Types

| Phase | Agent type (subagent_type=) | Purpose |
|-------|-----------|---------|
| Phase A | `agent-a` | First phase — describe purpose |
| Phase B | `agent-b` | Second phase — describe purpose |

## Loop Structure

```
LOOP:
  Phase A: agent-a -> read report -> decide next phase
  Phase B: agent-b -> read report -> back to Phase A
```

## Phase A

```
Agent(subagent_type="agent-a", prompt="Your instructions here. Write report to configured path.")
```

## Phase B

```
Agent(subagent_type="agent-b", prompt="Your instructions here.")
```

## Constraints

- Forward reports VERBATIM — do not rephrase
- ONE dispatch per agent per iteration
