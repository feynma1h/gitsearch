-- Migration 0008: skinny tsvector for the FTS lane's scoring pass
-- (ADR 0018).
--
-- 0007's `search_tsv` includes the README head (weight D), which is
-- right for *matching* — "turn markdown into a website" should reach a
-- repo whose README says exactly that — but wrong for *scoring at OR
-- breadth*: a broad query's any-term match covers tens of thousands of
-- rows, and every per-row score evaluation against the README-fat
-- column detoasts kilobytes (measured: ~11 s for one category query on
-- Micro compute).
--
-- The fix is a second, README-free tsvector: name + topics + language +
-- description only. It stays inline (no TOAST), so per-row coverage
-- checks across a 50K-row candidate set cost milliseconds. The FTS lane
-- matches against BOTH columns (light for any-term, fat for full-AND
-- README recall) but scores only against the light one.
--
-- Same operational notes as 0007: the ADD COLUMN rewrites the table
-- (minutes, AccessExclusiveLock; searches queue); run via the session
-- pooler with statement_timeout = 0.

SET search_path = public, extensions;
SET statement_timeout = 0;

CREATE OR REPLACE FUNCTION repo_search_tsv_light(
    full_name        text,
    topics           text[],
    primary_language text,
    description      text
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT setweight(to_tsvector('english',
               coalesce(translate(full_name, '/_', '  '), '')), 'A')
        || setweight(to_tsvector('english',
               coalesce(array_to_string(topics, ' '), '')
               || ' ' || coalesce(primary_language, '')), 'B')
        || setweight(to_tsvector('english', coalesce(description, '')), 'C')
$$;

ALTER TABLE repositories
    ADD COLUMN IF NOT EXISTS search_tsv_light tsvector
    GENERATED ALWAYS AS (
        repo_search_tsv_light(full_name, topics, primary_language, description)
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_repositories_search_tsv_light
    ON repositories USING GIN (search_tsv_light);
