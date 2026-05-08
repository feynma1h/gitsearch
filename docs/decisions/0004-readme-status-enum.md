# ADR 0004: `readme_status` distinguishes "not tried" from "no README"

**Status:** accepted
**Date:** 2026-05-03

## Context

The README pass needs to be resumable: if it crashes after fetching 5000
of 20000 READMEs, restarting should fetch only the remaining 15000, not
re-fetch the 5000 already done.

A naive resumability check is `WHERE readme IS NULL`. But this conflates
two states: (a) we haven't tried to fetch yet, and (b) we tried and the
repo genuinely has no README. State (b) is common — many repos exist
without READMEs.

If we conflate the two, every restart re-fetches every repo without a
README, burning rate-limit budget on guaranteed-empty results.

## Decision

Add a `readme_status` column with values `ok`, `not_found`, `empty`,
`error`. Resume with `WHERE readme_fetched_at IS NULL`, which is set
to `NOW()` after every attempt regardless of outcome.

- `ok` — fetched and stored
- `not_found` — repo has no README (404 or DMCA takedown)
- `empty` — file exists but is whitespace-only
- `error` — transient failure; retryable by clearing `readme_fetched_at`

## Alternatives considered

- **Single `readme IS NULL` check.** Simple but conflates states; wastes
  rate-limit on every restart.
- **A separate `readme_attempts` table** tracking each fetch try. More
  detail than needed; we only care about the latest outcome.
- **Boolean `readme_attempted` column.** Captures resume correctness but
  loses the *why* — useful for debugging ("how many of these are 404s?
  how many are transient errors?").

## Consequences

- ✅ Resume is cheap and correct.
- ✅ Easy to audit outcomes: `SELECT readme_status, COUNT(*) GROUP BY 1`.
- ✅ Easy to retry just the transient errors:
  `UPDATE repositories SET readme_fetched_at = NULL WHERE readme_status = 'error'`.
- ⚠️ The status vocabulary is informational, not enforced by a CHECK
  constraint. This is intentional — it lets us evolve the values
  without a migration. The cost is that typos go undetected.
- ⚠️ The indexer pipeline filters on `readme_status IS NOT NULL` (any
  attempted state) rather than `readme_status = 'ok'`. This means
  repos with `not_found` / `empty` / `error` *do* get embedded — using
  description, language, and topics only, since `document_builder.py`
  ([ADR 0008](0008-source-document-construction.md)) skips empty
  fields cleanly. The alternative (skip those repos entirely) would
  hide ~30-40% of the corpus from search, which is worse than
  embedding them on metadata alone.

## What would change this decision

- The status set grows to >10 values, at which point a CHECK constraint
  or lookup table starts paying for itself.
- We need per-attempt history (e.g., for SLA reporting). Switch to a
  separate `readme_fetch_attempts` table.
