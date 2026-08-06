-- Migration 0009: repository_enrichment — index-time document enrichment
-- (search v2 phase 2; ADR 0020).
--
-- Enrichment text (mined awesome-list descriptions, and later LLM
-- doc2query output) manufactures the category vocabulary canonical
-- repos' own metadata lacks ("machine learning framework python" must
-- reach pytorch, whose fields never say "framework"). It lives in its
-- OWN table, never merged into `repositories`' source columns, so that:
--
--   1. Provenance is explicit per row: which source produced the text,
--      with which model and prompt revision, when.
--   2. Removal is ONE migration: drop this table (and its function +
--      aggregate) and the corpus is byte-for-byte back to phase 1.
--      `repositories`, its generated tsvector columns (0007/0008), and
--      `repository_embeddings` are untouched by this file.
--   3. Absence degrades gracefully: every serving-path reference is a
--      LEFT JOIN / union against this table, so an empty table is
--      exactly phase-1 behaviour, repo by repo.
--
-- Postgres note, because the phase-2 plan sketched a different wiring:
-- the 0007/0008 generated columns CANNOT reference this table — a
-- generated column's expression must be immutable and row-local, and
-- cross-table lookups are neither. Wiring enrichment through those
-- functions would mean lying about immutability (the column would
-- silently go stale when enrichment changes) plus a full table rewrite
-- of `repositories` on every add AND on removal. Instead the enrichment
-- carries its own generated tsvector here, and the search service's
-- FTS lane unions/joins it at query time (search/service/db.py, behind
-- its PHASE2_RETRIEVAL flag). Same reversibility guarantee, honest
-- mechanics, and no rewrite of the big table in either direction.
--
-- Operational notes for the production (Supabase) run:
--   - No table rewrite of `repositories`. The two expression btrees at
--     the bottom build in seconds at 267K rows but take a SHARE lock
--     (writes queue, reads flow); run via the session pooler at a calm
--     moment anyway.
--   - Same extensions-schema handling as 0007; nothing here needs an
--     extension beyond core.

SET search_path = public, extensions;
SET statement_timeout = 0;

-- The enrichment document's weighting policy, one definition (the
-- generated column below calls it; backfills and debugging sessions can
-- call it directly). Weights mirror the 0007 philosophy — "how a person
-- names the thing they want":
--   A  aliases — name-analogs (awesome-list anchor texts like
--      "Next.js"; LLM-provided alternate names). Peers of 0007's name
--      tokens.
--   B  categories + synthetic queries — the category vocabulary slot.
--      Awesome-list section trails ("Frameworks", "Machine Learning")
--      are human-curated topic labels; Doc2Query-- synthetic queries
--      serve the same matching role. Peers of 0007's topics.
--   C  description — mined entry descriptions / LLM's one-paragraph
--      "what is this for". Peers of 0007's description.
--
-- No README-weight-D analog and no "light" twin: the enrichment payload
-- is short and README-free by construction, so one column serves both
-- matching and scoring (the 0008 detoast problem cannot recur here; the
-- STORAGE MAIN below keeps the tsvector inline and compressed).
CREATE OR REPLACE FUNCTION repo_enrichment_tsv(
    description text,
    queries     text[],
    aliases     text[],
    categories  text[]
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT setweight(to_tsvector('english',
               coalesce(array_to_string(aliases, ' '), '')), 'A')
        || setweight(to_tsvector('english',
               coalesce(array_to_string(categories, ' '), '')
               || ' ' || coalesce(array_to_string(queries, ' '), '')), 'B')
        || setweight(to_tsvector('english', coalesce(description, '')), 'C')
$$;

-- One row per (repo, source). Sources are closed-enum by CHECK so a
-- typo'd backfill can't silently create a third population:
--   'awesome-mined' — parsed from awesome-list READMEs; human-written
--                     anchor-text-analog descriptions. model and
--                     prompt_version are NULL.
--   'llm'           — generated (Doc2Query-- filtered); model and
--                     prompt_version record exactly what produced it,
--                     so a prompt revision can re-generate selectively.
CREATE TABLE IF NOT EXISTS repository_enrichment (
    repo_id        TEXT        NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    source         TEXT        NOT NULL CHECK (source IN ('awesome-mined', 'llm')),
    description    TEXT,
    queries        TEXT[]      NOT NULL DEFAULT '{}',
    aliases        TEXT[]      NOT NULL DEFAULT '{}',
    categories     TEXT[]      NOT NULL DEFAULT '{}',
    model          TEXT,
    prompt_version TEXT,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_tsv     tsvector    GENERATED ALWAYS AS (
        repo_enrichment_tsv(description, queries, aliases, categories)
    ) STORED,
    PRIMARY KEY (repo_id, source)
);

-- Keep the tsvector inline (compressed) rather than TOASTed: the FTS
-- lane evaluates per-row coverage against it across the whole matching
-- set, and 0008 measured what per-row detoasting does to that pass.
-- Enrichment payloads are a few hundred lexemes at most, so MAIN fits.
ALTER TABLE repository_enrichment
    ALTER COLUMN search_tsv SET STORAGE MAIN;

CREATE INDEX IF NOT EXISTS idx_repository_enrichment_tsv
    ON repository_enrichment USING GIN (search_tsv);

-- Fold a repo's enrichment rows (at most one per source) into one
-- tsvector at query time. tsvector_concat is core Postgres (it backs
-- the || operator; positions shift, weights survive); the aggregate
-- wrapper is what lets the FTS lane GROUP BY repo_id. No IF NOT EXISTS
-- for aggregates, hence the guard.
DO $do$
BEGIN
    CREATE AGGREGATE tsvector_agg (tsvector) (
        SFUNC    = tsvector_concat,
        STYPE    = tsvector,
        INITCOND = ''
    );
EXCEPTION WHEN duplicate_function THEN
    NULL;
END
$do$;

-- Punctuation-normalised name lookups, for the exact-name rule's alias
-- fix (ADR 0018 recorded the landmine: "nextjs" pins a squatter repo
-- literally named nextjs above vercel/next.js). Normalising [-._] out
-- of both sides makes vercel/next.js an exact-name hit for "nextjs"
-- too; ties inside the pinned tier already break by score-then-stars,
-- which the canonical repo wins. Derived from the repo's own name —
-- no enrichment dependency, works for every repo from day one.
CREATE INDEX IF NOT EXISTS idx_repositories_name_norm
    ON repositories (translate(lower(name), '-._', ''));
CREATE INDEX IF NOT EXISTS idx_repositories_full_name_norm
    ON repositories (translate(lower(full_name), '-._', ''));

-- ---------------------------------------------------------------------------
-- REMOVAL (the one-migration rollback this design guarantees):
--
--   1. Point serving back at phase-1 behaviour first — set
--      PHASE2_RETRIEVAL=off on the search service (config change, no
--      code deploy; see search/service/config.py).
--   2. Then:
--        DROP TABLE IF EXISTS repository_enrichment;
--        DROP AGGREGATE IF EXISTS tsvector_agg(tsvector);
--        DROP FUNCTION IF EXISTS repo_enrichment_tsv(text, text[], text[], text[]);
--        DROP INDEX IF EXISTS idx_repositories_name_norm;
--        DROP INDEX IF EXISTS idx_repositories_full_name_norm;
--   3. Enriched embeddings are separate rows under their own
--      model_name label (ADR 0006 keying) — remove with
--        DELETE FROM repository_embeddings WHERE model_name = '<label>';
--      after flipping EMBEDDINGS_MODEL_LABEL back (also config-only).
-- ---------------------------------------------------------------------------
