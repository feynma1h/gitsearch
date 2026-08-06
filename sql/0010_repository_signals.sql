-- Migration 0010: repository_signals — external criticality signals
-- from deps.dev (search v2 phase 2; ADR 0019).
--
-- "How many published packages actually depend on this repo" is the
-- authority signal stars can't fake (6M purchased stars were documented
-- in 2024; nobody fakes being a dependency of half of PyPI). Source is
-- Google's deps.dev API: free, keyless, CC-BY, cache-permitted, keyed
-- exactly as our corpus (github.com/owner/repo). The OpenSSF Scorecard
-- comes back in the same responses, so it is stored too (display /
-- future demotion policy; not blended in this phase).
--
-- Same containment contract as 0009: own table, LEFT JOINed by the
-- serving path behind the PHASE2_RETRIEVAL flag, empty table == the
-- signal contributes nothing. Removal is the one commented DROP at the
-- bottom. No rewrite of `repositories` in either direction.

SET search_path = public, extensions;
SET statement_timeout = 0;

CREATE TABLE IF NOT EXISTS repository_signals (
    repo_id           TEXT        PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    -- Dependents of the repo's most-depended-on published package
    -- (a monorepo publishes many; the flagship carries the signal).
    -- NULL means "no published package found", which the blend treats
    -- as zero criticality — distinct from 0, "published but unused".
    dependent_count   INTEGER,
    -- "system:name" of the package that carried the max, for
    -- provenance and spot-checking ("pypi:torch", "npm:react").
    dependent_package TEXT,
    -- OpenSSF Scorecard overall score, 0..10, as deps.dev reports it.
    scorecard_score   REAL,
    scorecard_date    DATE,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- REMOVAL: set PHASE2_RETRIEVAL=off (or just criticality weight 0 —
-- the shipped default) on the search service, then:
--     DROP TABLE IF EXISTS repository_signals;
-- ---------------------------------------------------------------------------
