---
name: semantic-chunking
description: "Heading-aware chunking to fix wrong-chunk-right-doc failures at 0.90"
---

# Variant: Semantic Chunking

## Hypothesis

At precision_at_5=0.90, the 2 remaining failures (q-004, q-008) are both
"wrong chunk from right document" — the system finds the right document but
returns the wrong section. This happens because fixed-size chunking splits
content at arbitrary character boundaries, so the query-relevant content
(e.g., "429 rate limit") ends up in a chunk that's less semantically coherent
than the intro/overview chunk.

## Proposed technique

**Heading-aware markdown splitting**: Instead of splitting at fixed character
positions (1000 chars), split at markdown heading boundaries (##, ###). This
keeps semantic sections intact — a "Rate Limiting" section stays in one chunk
rather than being split across a boundary with unrelated content.

## Why this should work

For q-004 specifically:
- Query: "HTTP status code for rate limit"
- Current: Chunk 0 (intro) ranks higher because it has broad topic coverage.
  Chunk 1 has "429" but it's mixed with other error details.
- With heading-aware chunking: The "Rate Limiting" section becomes its own
  chunk, so "429" is in a focused, semantically relevant chunk that should
  rank higher than a broad intro.

For q-008:
- The inner worker noted the gold label may be misaligned. If the retrieved
  chunk actually contains the answer, this is an eval set issue, not a
  retrieval issue. Investigate but don't optimize around a possibly-wrong label.

## Secondary technique: Query term boosting

If heading-aware chunking alone doesn't fix q-004, add a post-retrieval
boost: check if distinctive query terms (non-stop-words) appear literally in
retrieved chunks. Boost chunks with exact keyword matches. This would boost
the chunk containing "429" for the "rate limit status code" query.

## What NOT to try

- More threshold tuning (exhausted — 20 iterations of this already)
- More overlap adjustments (already at 400 chars forward)
- Changing BM25/vector weights (already optimized via RRF)

## Expected outcome

- q-004: Fixed by heading-aware chunking (section with "429" becomes own chunk)
- q-008: Investigate gold label; may be unfixable without eval set change
- Target: precision_at_5 >= 0.95
