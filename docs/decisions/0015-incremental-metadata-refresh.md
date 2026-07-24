# ADR 0015: Incremental metadata refresh

**Status:** accepted
**Date:** 2026-07-24

## Context

The corpus is populated once by a full metadata crawl (every star-range
shard, ~280K repos). Keeping it fresh, however, does not require re-pulling
the whole thing each week. The overwhelming majority of repos don't change
between runs, so a full weekly re-crawl spends ~5 hours of CI time (and a
lot of secondary-rate-limit exposure — see ADR 0001) re-fetching data that
is already current.

What actually changes week to week is a small tail: repos with new commits,
and brand-new repos that have crossed the star floor. GitHub's search API
lets us ask for exactly that slice with a `pushed:` qualifier.

## Decision

Run the metadata crawl **incrementally** by default in CI:

1. A single-row `crawl_state` table (migration 0005) records the start time
   of the last successful crawl — the *watermark*.
2. On an incremental run, every star-range shard query gains a
   ` pushed:>=<watermark-date>` qualifier
   (`shard_generator.apply_pushed_since`), so GitHub only returns repos in
   that band pushed since the watermark. The existing upsert
   (`db.insert_batch`, `ON CONFLICT DO UPDATE`) merges the results.
3. The watermark is advanced to the **run's start time** on clean
   completion (`main.set_last_crawl_at`), so the next run re-includes
   anything pushed while this one was in flight. Day granularity on the
   qualifier adds a further harmless overlap.
4. The **first** run (no watermark) and a **periodic full re-baseline**
   run with no `pushed:` filter. The full crawl also refreshes star counts
   on repos that changed rank without a new push — see below.

CI wiring: `refresh-metadata.yml` runs incrementally on the weekly cron and
does a full re-baseline on a monthly cron (or a manual `full=true`
dispatch). Locally, `make crawl` is full and `make crawl-incremental` is
incremental.

## Alternatives considered

- **`pushed:` + `created:` combined.** New repos are, in practice, also
  recently *pushed*, so `pushed:>=` already captures them. Adding a separate
  `created:` pass roughly doubles the query count for near-zero extra
  coverage. Rejected as not worth it; revisit if new-repo coverage proves
  lacking.
- **Per-repo GraphQL node lookups** (re-fetch each known repo by id on a
  cadence). Precise for star drift, but ~280K node lookups per refresh is
  far more expensive than the shard scan and buys little over a periodic
  full crawl. Rejected.
- **Event / webhook-driven ingestion** (GitHub Events API or webhooks).
  Genuinely incremental and real-time, but a large increase in operational
  surface for a batch-shaped project. Out of scope; noted as a future
  direction.
- **Pushed-only, no periodic full re-baseline.** Simpler, but stars change
  without a push, so a repo that gains stars (moving between bands or
  changing rank) would never be re-read until it's pushed again. Its stored
  star count — a ranking signal — would go stale. Rejected in favour of the
  periodic full pass.

## Consequences

- ✅ Weekly refresh drops from a full ~280K-repo scan to the small changed
  tail — far less CI time and far less secondary-rate-limit exposure.
- ✅ No schema change to `repositories`; the incremental path reuses the
  same upsert. Only a one-row `crawl_state` table is added.
- ✅ Safe by construction: watermark advances only on clean completion, so
  an interrupted run is retried from the same point; overlaps are idempotent
  under upsert.
- ✅ Degrades gracefully: if `crawl_state` is missing (migration not yet
  applied), the read is caught and the run falls back to a full crawl.
- ⚠️ **Star-count drift between full re-baselines.** Stars on a repo with no
  new commits aren't refreshed until the next full pass. The monthly full
  re-baseline bounds this staleness; adjust the cadence if rankings feel
  stale sooner.
- ⚠️ **Deletions and archival aren't detected incrementally.** A repo that
  is deleted or drops below the star floor simply stops appearing in
  results' inputs; its row lingers until a full re-baseline (which still
  won't delete it — it just won't be re-fetched). Acceptable for a search
  index; a reconciliation/GC pass is a possible future addition.

## What would change this decision

- New-repo coverage proves insufficient (new repos aren't appearing) →
  add a `created:` pass.
- Ranking quality visibly suffers from star drift between re-baselines →
  shorten the full-crawl cadence, or add a lightweight stars-only refresh.
- The project moves to near-real-time freshness expectations → adopt
  event/webhook-driven ingestion.
