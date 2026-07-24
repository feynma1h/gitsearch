-- Migration 0005: crawl_state.
--
-- Single-row table that records when the metadata crawl last ran. The
-- crawler uses it to run *incrementally*: after a one-time full crawl,
-- each subsequent run only asks GitHub for repos pushed since this
-- timestamp (via a `pushed:>=` qualifier on each star-range shard),
-- instead of re-pulling the whole corpus. The row is updated in place
-- after a successful run, so the table never grows beyond one row.
--
-- A NULL (or missing) watermark means "no full crawl has completed yet"
-- — the crawler falls back to a full crawl in that case.
--
-- See ADR 0015 (incremental metadata refresh) for the rationale, the
-- pushed-vs-created choice, and how star-count drift is handled by a
-- periodic full re-baseline.

CREATE TABLE IF NOT EXISTS crawl_state (
    id                      integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_metadata_crawl_at  timestamptz,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE crawl_state IS
    'Single-row table holding the start time of the most recent successful '
    'metadata crawl. Read/written by the crawler to drive incremental refresh.';
