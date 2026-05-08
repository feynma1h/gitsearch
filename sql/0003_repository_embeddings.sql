-- Migration 0003: pgvector extension + repository_embeddings table.
--
-- Embeddings live in a separate table rather than a column on
-- ``repositories`` so we can:
--   1. Run multiple models side-by-side for A/B comparison
--   2. Track when each embedding was generated
--   3. Drop and re-embed without disturbing the metadata
-- See ADR 0005 for the full rationale.
--
-- Vector dimension is 384 to match BAAI/bge-small-en-v1.5 (ADR 0006).
-- HNSW is used over IVFFlat (ADR 0009).
--
-- Run order for a fresh setup:
--   1. Apply this migration (creates extension + table; NO HNSW index yet).
--   2. Run the indexer pipeline to populate rows.
--   3. Run the HNSW build statement at the bottom of this file separately,
--      AFTER bulk insert completes. Building HNSW alongside bulk inserts is
--      roughly 10x slower than building once at the end.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS repository_embeddings (
    repo_id     TEXT        NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    model_name  TEXT        NOT NULL,
    embedding   vector(384) NOT NULL,
    -- Stable hash of the source text. Lets us detect "this repo's metadata or
    -- README changed since we embedded it" without re-embedding everything.
    source_hash TEXT        NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_id, model_name)
);

-- B-tree index on model_name for resume queries:
--   SELECT r.id FROM repositories r
--   LEFT JOIN repository_embeddings e
--          ON e.repo_id = r.id AND e.model_name = $1
--   WHERE e.repo_id IS NULL ORDER BY r.stars DESC LIMIT $2;
CREATE INDEX IF NOT EXISTS idx_repository_embeddings_model
    ON repository_embeddings (model_name);

-- ---------------------------------------------------------------------------
-- HNSW INDEX — DO NOT INCLUDE IN THE INITIAL MIGRATION RUN.
-- ---------------------------------------------------------------------------
--
-- After the indexer has populated the table, run this once:
--
--   CREATE INDEX CONCURRENTLY idx_repository_embeddings_hnsw
--       ON repository_embeddings
--       USING hnsw (embedding vector_cosine_ops)
--       WITH (m = 16, ef_construction = 64);
--
-- Tuning notes:
--   - m = 16 and ef_construction = 64 are pgvector's defaults; good
--     recall/QPS tradeoff for our ~100K-vector scale.
--   - Raise ef_construction (e.g., to 128) for better recall at index
--     build cost. Build is one-time; query latency is forever.
--   - Raise m (e.g., to 32) for better recall at memory cost.
--   - At query time, set ef_search higher than the default 40 if recall
--     is lacking: SET hnsw.ef_search = 100;
