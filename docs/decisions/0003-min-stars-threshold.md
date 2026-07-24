# ADR 0003: Default min-stars threshold of 200

**Status:** accepted
**Date:** 2026-05-03

> **Note (2026-05):** GitHub's population above 200 stars has since grown to
> ~280K repos — exactly the growth this ADR's *What would change this decision*
> section anticipated. The 200 floor is retained (the corpus stays tractable at
> ~280K, and only a curated ~20K are embedded), so the figures below reflect the
> original ~100K-era estimate rather than the current count.

## Context

The crawler's target corpus is "the top ~100K repos by stars." Earlier the
default `--min-stars` was 50, which captures roughly 500K repos — 5x the
target. Most of the extra crawl is wasted: we don't index them, we don't
embed them, they sit unused in the database.

## Decision

Default `--min-stars` to **200**, which captures approximately 100K repos.
The flag is still configurable; users wanting full coverage (or a smaller
corpus) can override it.

## Alternatives considered

- **Keep the floor at 50, just stop crawling once 100K repos are
  collected.** Wastes the same effort and adds complexity (which 100K?
  highest stars? oldest? randomly sampled?). Cleaner to choose the
  threshold that *defines* the corpus.
- **Crawl everything, query subsets later.** 5x more storage, 5x more
  crawl time, no real upside for the project's stated goal.
- **Make the threshold dynamic** (binary-search the right floor that
  yields exactly 100K). Over-engineered. The threshold drifts slowly
  enough (~weekly) that a hardcoded value is fine.

## Consequences

- ✅ ~5x less crawl work (~1 minute vs ~4 minutes for the metadata pass).
- ✅ ~5x less storage and embedding cost downstream.
- ✅ Simple, explicit number that anyone can change.
- ⚠️ The exact repo count drifts as projects gain/lose stars. 200 yields
  *approximately* 100K but the real number could be 90K or 120K at any
  given time. This is acceptable — the project's value isn't in hitting
  exactly 100K.
- ⚠️ Coupling with [ADR 0001](0001-sharded-star-range-crawling.md): the
  shard-width schedule is calibrated for `--min-stars >= 50`, which the
  200 default comfortably satisfies. If a future contributor lowers the
  floor below ~50, the bottom-most shards will hit the 1000-result cap
  and silently lose data — the shard generator must be revisited at the
  same time.

## What would change this decision

- The corpus needs to extend below 200 stars to capture niche tools. In
  that case lower the threshold and accept the longer crawl.
- GitHub's repo population grows enough that 200 stars yields >>100K.
  Bump the threshold to compensate.
