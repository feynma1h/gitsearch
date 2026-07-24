-- Migration 0006: repository_guides.
--
-- Cache for the per-repo "how do I use this?" usage guides. Each guide is
-- a short, standard step-by-step summary generated once from the repo's
-- README by a small language model, then served from here on every later
-- request — so a repo is only ever paid for (in tokens) the first time
-- someone clicks it.
--
-- `source_readme_fetched_at` records which README revision the guide was
-- built from. When the crawler re-fetches a repo's README (a newer
-- `repositories.readme_fetched_at`), the cached guide can be treated as
-- stale and regenerated.
--
-- See ADR 0016 (LLM-generated repository usage guides).

CREATE TABLE IF NOT EXISTS repository_guides (
    repo_id                   TEXT        PRIMARY KEY
                                          REFERENCES repositories (id) ON DELETE CASCADE,
    guide                     TEXT        NOT NULL,
    model_name                TEXT        NOT NULL,
    source_readme_fetched_at  TIMESTAMPTZ,
    generated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE repository_guides IS
    'Lazily-generated, cached usage guides (one per repo). Populated on first '
    'request by the search service; see ADR 0016.';
