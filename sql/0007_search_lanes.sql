-- Migration 0007: lexical + fuzzy retrieval lanes (ADR 0018).
--
-- Adds the two index structures the three-lane hybrid retrieval needs
-- beyond the existing HNSW:
--
--   1. A weighted, generated tsvector column over an enriched view of
--      each repo (name tokens > topics/language > description > README
--      head) with a GIN index — the full-text lane.
--   2. pg_trgm + a trigram GIN index on `name`, plus two btree indexes
--      for exact/prefix lookups — the name lane (typos, partial names).
--
-- The halfvec HNSW index is NOT here: like the original HNSW (see
-- 0003), vector index builds need session-scoped tuning and run for
-- minutes, so they live in `make build-hnsw-halfvec`.
--
-- Operational notes for the production (Supabase) run:
--   - ADD COLUMN ... GENERATED ... STORED rewrites the whole table
--     under AccessExclusiveLock. At ~900 MB (readmes included) expect
--     minutes; searches queue meanwhile. Run at a quiet moment via the
--     session pooler (port 5432) with statement_timeout = 0.
--   - Supabase installs extensions into the `extensions` schema; plain
--     Postgres (docker compose) uses `public`. The DO block below
--     handles both; the search_path set here covers unqualified
--     references either way (both schemas are on the default
--     search_path in both environments).

SET search_path = public, extensions;
SET statement_timeout = 0;

DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'extensions') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
    ELSE
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    END IF;
END
$do$;

-- The lexical document, as a function so the weighting policy has one
-- definition (the generated column below calls it, and any backfill or
-- debugging session can call it directly).
--
-- Weights follow "how a person names the thing they want":
--   A  name tokens — `translate` splits owner/name and snake_case;
--      to_tsvector itself splits hyphenated compounds (scikit-learn ->
--      scikit-learn + scikit + learn).
--   B  topics + primary language — GitHub topics carry exactly the
--      category vocabulary ("machine-learning", "vector-database")
--      that descriptions of canonical repos often lack.
--   C  description.
--   D  the first 2,500 chars of the README — enough for the elevator
--      pitch, small enough to keep the tsvector and its GIN index lean.
--
-- Declared IMMUTABLE so it can back a generated column. Everything it
-- calls is from pg_catalog and behaviour-stable; array_to_string is
-- formally STABLE (custom element output functions could vary) but is
-- immutable for text[], which is what topics is.
CREATE OR REPLACE FUNCTION repo_search_tsv(
    full_name        text,
    topics           text[],
    primary_language text,
    description      text,
    readme           text
) RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT setweight(to_tsvector('english',
               coalesce(translate(full_name, '/_', '  '), '')), 'A')
        || setweight(to_tsvector('english',
               coalesce(array_to_string(topics, ' '), '')
               || ' ' || coalesce(primary_language, '')), 'B')
        || setweight(to_tsvector('english', coalesce(description, '')), 'C')
        || setweight(to_tsvector('english',
               left(coalesce(readme, ''), 2500)), 'D')
$$;

-- Generated so it can never drift from the row (the crawler's periodic
-- refreshes rewrite repos wholesale; a trigger-maintained column would
-- be one more thing to keep honest). This statement is the table
-- rewrite called out above.
ALTER TABLE repositories
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        repo_search_tsv(full_name, topics, primary_language, description, readme)
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_repositories_search_tsv
    ON repositories USING GIN (search_tsv);

-- Name lane. The trigram index serves similarity (`%`) matches — typos
-- like "pytorhc"; the btrees serve exact and prefix lookups (the lane
-- queries `lower(full_name) = $q` and `lower(name) LIKE 'q%'`;
-- text_pattern_ops makes the left-anchored LIKE indexable regardless
-- of collation).
CREATE INDEX IF NOT EXISTS idx_repositories_name_trgm
    ON repositories USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_repositories_full_name_lower
    ON repositories (lower(full_name));
CREATE INDEX IF NOT EXISTS idx_repositories_name_lower
    ON repositories (lower(name) text_pattern_ops);
