# ADR 0011: HNSW over IVFFlat for the vector index

**Status:** accepted
**Date:** 2026-05-03

## Context

pgvector offers two index types for approximate nearest-neighbor search:

- **IVFFlat** (older). Partitions vectors into `lists` clusters; queries
  scan the closest `probes` clusters. Requires "training" the partitions
  on a representative sample, and the partitions degrade as data grows
  beyond the training set.
- **HNSW** (newer, since pgvector 0.5.0). Builds a multi-layer graph
  incrementally. No training step; queries traverse the graph from a
  high-level entry point downward.

## Decision

Use **HNSW** with default parameters (`m = 16`, `ef_construction = 64`)
and `vector_cosine_ops` (cosine distance, since our embeddings are
normalized).

## Alternatives considered

- **IVFFlat.** Older, more battle-tested, lower index build memory.
  But the training requirement adds operational complexity (rebuild
  required after meaningful data growth), and recall/QPS are dominated
  by HNSW on most modern benchmarks.
- **No index, exact nearest-neighbor.** With 100K vectors and 384 dims,
  this is ~150ms per query — borderline acceptable but visibly slow.
  Index brings it under 5ms.

## Consequences

- ✅ Builds incrementally — new embeddings are inserted into the index
  online, no rebuild needed.
- ✅ Strong recall/QPS tradeoff out of the box.
- ⚠️ Higher build-time memory than IVFFlat. For 100K x 384-dim vectors,
  ~200MB peak. Fits comfortably in any modest dev environment.
- ⚠️ Build is slower if done concurrent with bulk inserts. We `CREATE
  INDEX CONCURRENTLY` *after* the bulk embedding pass, not during.

## Implementation note

Build the index in this order:

1. Bulk-insert all embeddings (no index — fast inserts).
2. `CREATE INDEX CONCURRENTLY ... USING hnsw (...)`.

Building during inserts is ~10x slower per row.

This is why the project's `Makefile` has `index` and `build-hnsw` as
separate targets rather than a single "set up search" command. Running
the indexer pipeline and the index build concurrently fights — the
pipeline keeps inserting rows that the index has to incorporate online,
and the index build blocks on each new batch. Pre-existing index also
slows the bulk insert path. The two phases are deliberately serial.

## What would change this decision

- Vector count grows past ~10M and HNSW build time becomes painful.
  IVFFlat scales to larger corpora when you can tolerate the rebuild
  cycle.
- pgvector adds a better index type. The migration is mechanical
  (drop old index, create new one).
