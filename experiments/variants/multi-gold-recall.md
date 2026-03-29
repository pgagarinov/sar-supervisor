---
name: start
description: "Fix multi-gold recall: retrieve more results for multi-topic queries"
user_invocable: true
---

# /start -- Multi-Gold Recall Fix

## ORCHESTRATOR RULES — READ FIRST, OBEY ALWAYS

You are a **PURE DISPATCHER**. You launch agents and read their reports. That is ALL.

### PERMITTED (exhaustive):
- `Agent(subagent_type="evaluator", prompt="...")` — dispatch evaluator
- `Agent(subagent_type="improver", prompt="...")` — dispatch improver
- `Bash("cd ../sar-rag-target && git reset --hard HEAD~1")` — discard experiment
- Print text summaries of iteration results

### FORBIDDEN:
- `Read`, `Grep`, `Glob` on ANY file
- `Bash` to inspect files, run commands, check git, or analyze code
- `Edit` or `Write` on ANY file
- Summarizing or rephrasing agent output — forward VERBATIM
- `TodoWrite`

**Before every tool call, check: "Am I dispatching an agent or discarding via git reset?" If NO, stop.**

## CURRENT STATE

**Baseline: recall_at_5 ≈ 0.91, precision_at_5 = 1.0**

Your FIRST action: dispatch evaluator to get fresh metrics and the per-question breakdown.

## THE PROBLEM

At recall=0.91, the remaining failures are ALL multi-gold queries — queries where multiple chunks from different documents are relevant. The system retrieves 1-2 chunks but misses others.

**Failure class A (3 queries):** Cross-document miss. The system retrieves from 1-2 document families but not all relevant ones. The diversity logic either doesn't add enough cross-doc results, or blocks valid candidates.

**Failure class B (1 query):** Same-document miss. Two chunks from the same document are both gold, but only one is retrieved. Cross-doc diversity cannot help here.

## APPROACH — TWO SEQUENTIAL FIXES

### Fix 1: Same-doc adjacent chunk inclusion (targets class B)
After selecting the primary result for a document, check if an adjacent chunk (±1 index) from the same document also appears in the retrieval pool with a score within 90% of the primary. If so, include it. This is low-risk because same-doc adjacency is a strong relevance signal.

### Fix 2: Allow multiple cross-doc additions (targets class A)
The current diversity logic may stop after adding one cross-doc result. Allow up to 2 cross-doc additions, each independently passing all existing quality filters.

**Try Fix 1 first.** If it works (recall improves, precision stays >= 0.95), keep and move to Fix 2. If it fails, discard and try Fix 2 directly.

## EXHAUSTED — DO NOT REPEAT
1. Score-ratio threshold tuning (tried 6+ values)
2. Query-term novelty filtering
3. MIN_GAP / MIN_SCORE adjustments (20+ iterations)
4. Heading-aware chunking (caused regression)
5. Rewriting chunker splitting logic

## THE LOOP

```
FOREVER:
  1. EVALUATE — dispatch evaluator, get full JSON report
  2. ANALYZE — identify failing queries from per_question results
  3. HYPOTHESIZE — pick ONE fix targeting the dominant failure class
  4. IMPROVE — dispatch improver with full report + hypothesis
  5. EVALUATE — dispatch evaluator, get new metrics
  6. DECIDE:
     - KEEP if recall_at_5 improved AND precision_at_5 >= 0.95
     - DISCARD if not: cd ../sar-rag-target && git reset --hard HEAD~1
  7. LOG — print: "Iteration N: [technique], recall X→Y, precision Z, KEPT/DISCARDED"
  8. REPEAT
```

## AGENT DISPATCH TEMPLATES

### Evaluator
```
Agent(subagent_type="evaluator", prompt="Run evaluation on target at ../sar-rag-target. Steps: rm -rf /tmp/fluxapi-chroma && cd ../sar-rag-target && pixi run eval. Then cat /tmp/rag-eval-report.json. Report the FULL JSON including per_question results. Also: cd ../sar-rag-target && git log --oneline -3")
```

### Evaluator + Code Inspection
```
Agent(subagent_type="evaluator", prompt="Target: ../sar-rag-target. Read and report FULL contents of: ../sar-rag-target/src/rag/retriever.py, ../sar-rag-target/src/rag/reranker.py. Then run eval: rm -rf /tmp/fluxapi-chroma && cd ../sar-rag-target && pixi run eval. Report FULL /tmp/rag-eval-report.json. Also: cd ../sar-rag-target && git log --oneline -5")
```

### Improver
```
Agent(subagent_type="improver", prompt="Target: ../sar-rag-target. Current eval report (FULL): [PASTE COMPLETE JSON]. Hypothesis: [STATE HYPOTHESIS]. Task: Make ONE targeted change. precision_at_5 must stay >= 0.95. Read current code first. After editing, commit: cd ../sar-rag-target && git add -A && git commit -m 'experiment: [description]'")
```

## GUARD RAILS
- **Precision guard**: precision_at_5 >= 0.95 or DISCARD
- **One change per iteration**
- **Protected files**: NEVER modify corpus/, eval_set.json, src/rag/paths.py
- **Sequential**: ONE agent at a time, never overlap
