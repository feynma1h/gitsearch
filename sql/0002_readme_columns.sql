-- Migration 0002: add columns for the README fetching pass.
--
-- `readme_status` distinguishes "we haven't tried" (NULL) from "we tried and
-- there is no README" ('not_found') from "we tried and got it" ('ok'), so
-- restarts only re-attempt repos that genuinely need it.
--
-- `readme_fetched_at` is indexed because the resume query
--   SELECT ... WHERE readme_fetched_at IS NULL ORDER BY stars DESC LIMIT N
-- runs on every batch fetch and a partial index keeps it cheap as the table
-- fills up.

ALTER TABLE repositories
    ADD COLUMN IF NOT EXISTS readme_fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS readme_status     TEXT;

-- Partial index: we only ever query for unfetched rows, so don't waste
-- index space on the rest.
CREATE INDEX IF NOT EXISTS idx_repositories_readme_pending
    ON repositories (stars DESC)
    WHERE readme_fetched_at IS NULL;

-- Allowed status values (informational; not enforced by a CHECK so we can
-- evolve the vocabulary without another migration).
COMMENT ON COLUMN repositories.readme_status IS
    'NULL=not attempted; ok=fetched; not_found=no README in repo; '
    'empty=README exists but blank; error=transient failure (retryable).';
