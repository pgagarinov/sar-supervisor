---
name: start
description: "Improve recall while keeping precision >= 0.95"
user_invocable: true
---

# /start -- Precision-Safe Recall Improvement

## ORCHESTRATOR RULES — READ FIRST, OBEY ALWAYS

You are a **PURE DISPATCHER**. You launch agents and read their reports. That is ALL.

### PERMITTED (exhaustive):
- `Agent(subagent_type="evaluator", prompt="...")` — dispatch evaluator
- `Agent(subagent_type="improver", prompt="...")` — dispatch improver
- `Bash("cd ../sar-rag-target && git log --oneline -5")` — check git state
- `Bash("cd ../sar-rag-target && git reset --hard <commit>")` — discard to known state
- Print text summaries of iteration results

### FORBIDDEN:
- `Read`, `Grep`, `Glob` on ANY file
- `Bash` to inspect files or run eval
- `Edit` or `Write` on ANY file
- Summarizing or rephrasing agent output — forward VERBATIM
- `TodoWrite`

## CURRENT STATE

**Baseline: recall_at_5 ≈ 0.91, precision_at_5 = 1.0**

Your FIRST action: dispatch evaluator to get fresh metrics. **Record the baseline HEAD commit hash from the evaluator's response.**

## CRITICAL: TRACKING COMMITS FOR SAFE DISCARD

The improver may make multiple commits. `git reset --hard HEAD~1` only undoes ONE.

**You MUST track the HEAD commit before each improver dispatch.**

```
Before improve: record BASELINE_HEAD from evaluator's git log
After improve: if discard needed, reset to BASELINE_HEAD (not HEAD~1):
  Bash("cd ../sar-rag-target && git reset --hard <BASELINE_HEAD>")
```

**NEVER use HEAD~1 for discard. ALWAYS reset to the recorded commit hash.**

## PRECISION GUARD — ABSOLUTE RULE

**precision_at_5 MUST stay >= 0.95 after EVERY change.** If it drops below 0.95:
1. IMMEDIATELY discard to BASELINE_HEAD
2. Re-evaluate to confirm precision recovered
3. Try a DIFFERENT approach

**You must NEVER keep a change that drops precision below 0.95, even if recall improves.** High recall with low precision means the system is returning junk results.

## BANNED APPROACHES — WILL DESTROY PRECISION

These approaches boost recall by returning more results, which tanks precision. **DO NOT attempt them:**

1. **Expanding the retrieval pool** (increasing top_k, expansion_factor, k*N multiplier)
2. **Lowering similarity thresholds** (min_score, min_gap, score cutoffs)
3. **Disabling score-gap filtering** (removing the gap filter that trims results)
4. **Returning all k results instead of filtering** (the filter exists for precision)
5. Score-ratio threshold tuning (tried 6+ values, exhausted)
6. Query-term novelty filtering (tried, causes false positives)
7. Heading-aware chunking (caused 0.90→0.63 regression)

**The fix must come from SMARTER SELECTION, not MORE RESULTS.** The system should return the same number of results but pick better ones.

## THE PROBLEM

At recall=0.91, exactly 4 queries fail — all are multi-gold queries where the system retrieves some but not all relevant chunks. The system finds the right documents but picks the wrong chunks.

**Fix strategy:** For each document family in the result set, ensure the BEST chunk is selected. The current selection may use fusion score, but BM25 score or vector similarity might pick a better chunk within the same document.

## THE LOOP

```
FOREVER:
  1. EVALUATE — dispatch evaluator, get full JSON report
  2. ANALYZE — identify failing queries: which gold chunks are missing?
     For each miss: is the correct DOCUMENT retrieved but wrong CHUNK?
  3. HYPOTHESIZE — pick ONE change that fixes chunk selection WITHOUT
     expanding the result set. Focus on: which scoring signal (BM25,
     vector, fusion) best distinguishes the gold chunk from siblings?
  4. IMPROVE — dispatch improver with full report + hypothesis
  5. EVALUATE — dispatch evaluator, get new metrics
  6. DECIDE:
     - KEEP if recall_at_5 improved AND precision_at_5 >= 0.95
     - DISCARD if recall_at_5 did not improve OR precision_at_5 < 0.95
     - DISCARD: Bash("cd ../sar-rag-target && git reset --hard <BASELINE_HEAD>")
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
Agent(subagent_type="improver", prompt="Target: ../sar-rag-target. Current eval report (FULL): [PASTE COMPLETE JSON]. Hypothesis: [STATE HYPOTHESIS]. Task: Make ONE targeted change in ONE file. CRITICAL RULES: (1) precision_at_5 must stay >= 0.95 — do NOT expand the result pool, increase top_k, or lower thresholds. (2) Make exactly ONE commit — do NOT make multiple commits. Read the file, edit it, then: cd ../sar-rag-target && git add -A && git commit -m 'experiment: [description]'. STOP after that single commit.")
```

**CRITICAL: [PASTE COMPLETE JSON] means the ENTIRE evaluator output. Not a summary.**

## GUARD RAILS
- **Precision guard**: precision_at_5 >= 0.95 or IMMEDIATE DISCARD
- **No pool expansion**: NEVER increase top_k, expansion_factor, or k multipliers
- **One change per iteration**
- **Protected files**: NEVER modify corpus/, eval_set.json, src/rag/paths.py
- **Sequential**: ONE agent at a time, never overlap

## STAGNATION RECOVERY
- **3 consecutive discards**: Dispatch evaluator with code inspection template to read the full retriever/reranker code. Understand the pipeline before trying more changes.
- **5 consecutive discards**: The approach class is wrong. Try something structurally different.
