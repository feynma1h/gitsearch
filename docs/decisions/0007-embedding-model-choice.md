# ADR 0007: `bge-small-en-v1.5` as the embedding model

**Status:** accepted
**Date:** 2026-05-03

## Context

The embedding model determines retrieval quality, storage cost, and
runtime cost. Constraints:

- **Free** (no API spend during development).
- **Runs on CPU** (target deployment is a single small VM or a laptop).
- **Strong on retrieval** (the task is "given a natural-language query,
  rank relevant repos").
- **Reasonable dimensions** (lower = less storage, faster nearest-neighbor).

## Decision

Use **`BAAI/bge-small-en-v1.5`** via `sentence-transformers`.

- 384 dimensions
- ~33MB on disk
- ~50 docs/sec on a laptop CPU
- MTEB retrieval score within ~3 points of OpenAI `text-embedding-3-small`

## Alternatives considered

- **`bge-base-en-v1.5`** (768 dims, ~15 docs/sec on CPU). Slightly better
  quality, 2x storage, 3x slower. The quality gain isn't worth the
  throughput hit for a portfolio-scale corpus.
- **`nomic-embed-text-v1.5`** (768 dims, Matryoshka — can be truncated to
  256). Comparable quality, no clear win over bge-small at 384.
- **OpenAI `text-embedding-3-small`** (1536 dims, ~$2 for 100K repos).
  Strong, fast (API-bound), but violates the "free" constraint and 4x the
  storage. Worth keeping as an option for an A/B comparison later.
- **`all-MiniLM-L6-v2`** (384 dims, the classic sentence-transformers
  default). Older, lower MTEB scores. bge-small is a strict upgrade.

## Consequences

- ✅ Free, fast, fits the project's "no spend" constraint.
- ✅ 384-dim vectors are 4x smaller in pgvector than 1536-dim — both
  storage and HNSW query latency benefit.
- ✅ `sentence-transformers` is well-documented, easy to swap models if
  needed.
- ⚠️ Quality is slightly below state-of-the-art commercial APIs.
  Acceptable for a portfolio project; revisit if quality complaints
  emerge.
- ⚠️ ~500MB install footprint (PyTorch). The embedding service therefore
  has its own dependency set, separate from the indexer pipeline (ADR
  0008).

## What would change this decision

- Retrieval quality is measurably weak on real queries. Bench
  alternatives on a small labeled query set.
- We move to GPU inference. Larger models become viable.
- Budget loosens. OpenAI or Voyage models are worth A/B testing.
