# ADR 0006: Separate `repository_embeddings` table

**Status:** accepted
**Date:** 2026-05-03

## Context

Each repo needs an embedding vector for semantic search. Two natural
places to store it:

1. **Column on `repositories`**: `embedding vector(384)` directly on the
   existing table.
2. **Separate table** keyed by `(repo_id, model_name)`.

## Decision

Create a separate `repository_embeddings` table with columns
`(repo_id, model_name, embedding, embedded_at)` and a composite primary
key on `(repo_id, model_name)`.

## Alternatives considered

- **Single column on `repositories`.** Simpler schema, one row per repo.
  But: trying a new model means schema migration + backfill; can't run
  multiple models side-by-side for A/B comparison; no record of when an
  embedding was generated, which matters when content changes (README
  refetch) and embeddings need refreshing.
- **JSONB column with multiple model results.** Avoids the join but
  Postgres can't index jsonb fields as vectors — pgvector requires a
  typed `vector(N)` column. Defeats the point.

## Consequences

- ✅ A/B testing models is trivial — index with model A and B side by
  side, compare retrieval quality on the same query set.
- ✅ Re-embedding (after model upgrade or README refresh) is just
  `DELETE FROM repository_embeddings WHERE model_name = ...` followed
  by a re-index.
- ✅ `repositories` table stays stable; the embedding pipeline owns its
  own table.
- ⚠️ Joins required at query time. With proper indexes this is cheap,
  but it's one more thing to get right in the search API.
- ⚠️ Two writes per repo (metadata + embedding) instead of one update.
  Not meaningful at our scale.

## What would change this decision

- We commit permanently to one model and never want to A/B compare.
  Even then, the operational benefits of "embeddings are a separate
  artifact" probably keep the table separate.
