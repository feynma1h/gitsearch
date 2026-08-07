-- Migration 0001: repositories table.
--
-- `id` is the GitHub GraphQL node ID (a base64-encoded global ID), which is
-- stable across renames and ownership transfers. We index `full_name` for
-- lookups by "owner/repo" and `stars` for ranking.
--
-- `topics` is stored as a Postgres TEXT[] so we can filter with `&&` (overlap)
-- and `@>` (contains) without a join table. For 100K rows this is plenty fast.
--
-- `readme` is nullable because READMEs are fetched in a second pass, after
-- the metadata crawl completes.

CREATE TABLE IF NOT EXISTS repositories (
    id               TEXT        PRIMARY KEY,
    full_name        TEXT        NOT NULL UNIQUE,
    name             TEXT        NOT NULL,
    owner            TEXT        NOT NULL,
    description      TEXT,
    url              TEXT        NOT NULL,
    homepage_url     TEXT,
    primary_language TEXT,
    topics           TEXT[]      NOT NULL DEFAULT '{}',
    stars            INTEGER     NOT NULL DEFAULT 0,
    forks            INTEGER     NOT NULL DEFAULT 0,
    is_archived      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_fork          BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL,
    pushed_at        TIMESTAMPTZ,
    readme           TEXT,
    crawled_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_repositories_stars      ON repositories (stars DESC);
CREATE INDEX IF NOT EXISTS idx_repositories_pushed_at  ON repositories (pushed_at DESC);
CREATE INDEX IF NOT EXISTS idx_repositories_language   ON repositories (primary_language);
CREATE INDEX IF NOT EXISTS idx_repositories_topics_gin ON repositories USING GIN (topics);
