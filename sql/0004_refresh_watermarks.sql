-- Migration 0004: refresh_watermarks.
--
-- Single-row table that records the corpus-health counts after each
-- successful refresh. The chunked-refresh pipeline's regression check
-- (scripts/check_regression.py) compares the current run's counts
-- against this watermark and fails the workflow if any count drops
-- materially (>5%). The watermark is updated in place after a healthy
-- run, so the table never grows beyond one row.
--
-- See ADR 0014 (chunked GitHub Actions for corpus refresh) for the
-- rationale.

CREATE TABLE IF NOT EXISTS refresh_watermarks (
    id              integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    total_repos     integer NOT NULL,
    readme_success  integer NOT NULL,
    embeddings      integer NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE refresh_watermarks IS
    'Single-row table holding the corpus counts from the most recent '
    'successful refresh. Used by check_regression.py.';