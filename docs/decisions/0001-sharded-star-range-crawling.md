# ADR 0001: Sharded star-range crawling

**Status:** accepted
**Date:** 2026-05-03

## Context

GitHub's GraphQL search API caps any single query at **1000 results**, no
matter how the query is paginated. A naive "all repos sorted by stars"
query is therefore impossible — we'd see only the top 1000.

## Decision

Split the search space into many **non-overlapping star-range shards**
(`stars:50..59`, `stars:60..69`, ...) and process them concurrently.
Adjacent shards must not share endpoints because GitHub's `..` syntax is
inclusive on both ends.

Use **variable-width shards**: narrow at the bottom (where repos are
dense), wide at the top (sparse). Stars distribution is heavily skewed —
millions of repos at <10 stars, only thousands at >10K.

## Alternatives considered

- **Pagination only.** Hits the 1000-result cap, only retrieves the top.
- **Time-based sharding** (e.g., by `created_at` ranges). Works in
  principle but star count is what users care about, so it's the natural
  partition key. Time-sharding also doesn't help re-crawls (a repo's
  creation date is fixed; its stars change).
- **Uniform-width shards.** Either too narrow at the top (wasted queries)
  or too wide at the bottom (still hits the 1000 cap). Variable widths
  fit the actual distribution.

## Consequences

- ✅ Crawls past the 1000-result cap. ~100K repos in ~4 minutes on a
  single token.
- ✅ Naturally parallelisable — workers pull shards from a queue.
- ⚠️ Boundary correctness is subtle. The two failure modes to watch
  for: (a) writing `stars:50..59` and `stars:59..69` produces shards
  that *both* claim a 59-star repo, double-counting it; (b) writing
  `stars:50..58` and `stars:60..69` skips 59-star repos entirely. The
  generator emits `[lo, hi]` pairs with `next_lo = hi + 1` to avoid
  both. `tests/test_shard_generator.py` checks for overlaps and gaps
  by enumerating consecutive shard pairs — those tests are the only
  thing standing between us and a silent ~1% data-quality bug.
- ⚠️ Below ~50 stars, even single-star shards exceed the 1000-result
  cap. The exact crossover depends on GitHub's repo density at low
  star counts, which grows as GitHub grows. As of 2026, ~50 stars is
  the floor where uniform-width single-star shards fit; for the
  current `--min-stars=200` default ([ADR 0003](0003-min-stars-threshold.md))
  this is well within the comfortable range.

## What would change this decision

- GitHub raises or removes the 1000-result cap.
- We need to crawl repos with <50 stars (would require a different
  partition scheme — perhaps language + stars + date triples, since
  star-only buckets are too dense at the bottom of the distribution).
- The repo-density crossover migrates upward (GitHub's overall growth
  pushes more repos into low-star ranges). Bumping `--min-stars`
  ([ADR 0003](0003-min-stars-threshold.md)) is the simpler fix; only
  if we genuinely need the long tail does the partition scheme need
  to change.
