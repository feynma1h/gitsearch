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

- ✅ Crawls past the 1000-result cap. ~280K repos in ~25 minutes on a
  single token at the current `--workers=5` default with the
  density-calibrated band layout (see below).
- ✅ Naturally parallelisable — workers pull shards from a queue.
- ⚠️ Boundary correctness is subtle. The two failure modes to watch
  for: (a) writing `stars:50..59` and `stars:59..69` produces shards
  that *both* claim a 59-star repo, double-counting it; (b) writing
  `stars:50..58` and `stars:60..69` skips 59-star repos entirely. The
  generator emits `[lo, hi]` pairs with `next_lo = hi + 1` to avoid
  both. `tests/test_shard_generator.py` checks for overlaps and gaps
  by enumerating consecutive shard pairs — those tests are the only
  thing standing between us and a silent ~1% data-quality bug.
- ⚠️ **Shard widths must be density-calibrated, not picked by intuition.**
  The 1000-result cap is per-shard, applied silently. A shard whose
  underlying repo population exceeds 1000 returns its top 1000 by stars
  and drops the rest with no error — the crawl looks healthy and the
  long tail of that bucket is simply gone. An earlier version of this
  generator used 10-star shards across `200..1000` (e.g. `stars:200..209`).
  A bucket-distribution check at `--min-stars=50` revealed roughly
  this density:

  | Star range | Repos | Per-10-star avg |
  |------------|------:|----------------:|
  | 50-499     | 671K  | ~15K            |
  | 500-1.9K   | 86K   | ~3K             |
  | 2K-9.9K    | 26K   | ~3K (per 500)   |
  | 10K+       | 4.8K  | ~600 (per 5K)   |

  Even at `--min-stars=200`, a 10-star shard like `stars:200..209` covers
  ~14K repos and is clipped to 1000 — losing >90% of the bucket. The
  clipping persists, with diminishing severity, all the way up to
  ~stars 1000+. This bug is invisible without an explicit population
  audit because every individual shard returns a successful page of 1000
  results.

  The current band layout in `shard_generator.py` is sized so the
  average bucket in each band stays well under 1000, with the densest
  band (200-399) at the edge:

  | Band            | Width  | Shards | Avg bucket | Notes                            |
  |-----------------|-------:|-------:|-----------:|----------------------------------|
  | 200-399         |     1  |  200   | ~700       | densest end (~200) approaches cap |
  | 400-999         |     2  |  300   | ~600       |                                  |
  | 1000-1999       |    50  |   20   | ~150       |                                  |
  | 2000-4999       |   100  |   30   | ~100       |                                  |
  | 5000-9999       |   500  |   10   | ~250       |                                  |
  | 10000-49999     |  5000  |    8   | ~600       |                                  |

  Total: ~568 range shards plus the open-ended `stars:>=50000`. Wall
  time is ~25 minutes at `--workers=5` (up from ~12 minutes under the
  old undersized layout — the extra ~13 minutes is the cost of not
  silently dropping the long tail of the 200-999 range).
  `tests/test_shard_generator.py` pins these counts down explicitly.

  The 200-399 band is the one to keep an eye on: density falls
  monotonically with stars, so `stars:200..200` is the densest single
  shard in the entire crawl, plausibly in the 1000-1500 repo range. If
  a periodic population audit shows the very-low-star buckets crossing
  the 1000-result cap, the band needs to either (a) start at a higher
  `--min-stars` or (b) be replaced with a sub-star partition (e.g.
  splitting 200..200 by language).

- ⚠️ For `--min-stars` values significantly below 200, the table above
  no longer applies. The 50-199 region has roughly the same density
  per 10-star window as 200-399, so 1-star widths still fit; below 50,
  single-star shards exceed the 1000-result cap and a different
  partition scheme would be needed (language + stars + date triples,
  for example). The `--min-stars=200` default in [ADR 0003](0003-min-stars-threshold.md)
  is the explicit choice to stay above that floor.
- ⚠️ Concurrency is bounded by GitHub's *secondary* rate limit, not
  by the primary 5000 pts/hour budget. The secondary limit is
  undocumented, fires on burst/concurrent-request patterns, and
  surfaces only as HTTP 403 with `secondary rate limit` in the body.
  An earlier version of this code ran 15 concurrent workers and saw
  >80% of shards abort within minutes on the high-star end of the
  distribution (where shards are wider, queries are heavier, and
  fewer concurrent slots are available before tripping the limit).
  The fix was twofold: drop the default to 5 workers
  ([`config.py:DEFAULT_METADATA_WORKERS`](../../crawler/src/config.py)),
  and have `github_client` retry secondary-rate-limit responses
  with a 60s+ backoff that triggers a global pause on the shared
  `RateLimiter` so siblings also stop. Either fix alone is
  insufficient: high concurrency without backoff catastrophically
  fails; backoff without concurrency limits still wastes the first
  several minutes of a crawl recovering.

## What would change this decision

- GitHub raises or removes the 1000-result cap.
- We need to crawl repos with <50 stars (would require a different
  partition scheme — perhaps language + stars + date triples, since
  star-only buckets are too dense at the bottom of the distribution).
- The repo-density crossover migrates significantly. GitHub's overall
  growth pushes more repos into every star range; if a periodic
  population audit shows any band's worst-case bucket approaching the
  1000-result cap, recalibrate that band's width and update both the
  generator and the band-count test. Bumping `--min-stars`
  ([ADR 0003](0003-min-stars-threshold.md)) is the simpler escape
  hatch; only if we genuinely need the long tail does the partition
  scheme need to change.
