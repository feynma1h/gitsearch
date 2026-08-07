# ADR 0014: Chunked GitHub Actions for corpus refresh

**Status:** accepted
**Date:** 2026-05-10

> **Note (updated 2026-08):** the ~16-hour estimate for the README pass below
> is too low. At the measured 5000 requests/hour ceiling, a full pass over the
> ~267K-repo corpus is closer to ~55 hours, so anything sized off the 16-hour
> figure under-provisions by roughly 8 chunks. The chunking decision itself is
> unaffected — it only gets more necessary. See [the backlog](../backlog.md)
> for the conditional-refresh work that supersedes a full pass.

## Context

The corpus needs periodic refresh: a metadata re-crawl picks up new repos
and updated star counts, a README pass fetches READMEs for any new repos,
and the indexer pipeline embeds those new READMEs. At the early
~20K-repo target, all three stages fit comfortably inside one ~6-hour
GitHub Actions job. At the production ~280K-repo target they don't:
the README pass alone is ~16 hours and indexing is ~8 hours, both
well past the 6-hour single-job ceiling.

## Decision

Run the refresh as **three chained workflows**, where the README and
indexing stages **self-rechunk** inside a 5-hour-per-run budget until
the work is drained:

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  refresh-metadata    │────▶│  refresh-readme      │────▶│  refresh-index       │
│  cron: weekly        │     │  workflow_run trigger│     │  workflow_run trigger│
│  single ~5h job      │     │  self-rechunks       │     │  self-rechunks +     │
│                      │     │  until drained       │     │  regression check    │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

Stage A (metadata) is a single job: even at production scale the crawl
fits inside the chunk budget. Stages B and C use a **time-bounded**
chunk model: each run processes whatever it can in 5 hours and exits
cleanly; the next chunk picks up where it left off because both stages
filter on existing-row predicates (`readme_fetched_at IS NULL` and
`LEFT JOIN repository_embeddings WHERE repo_id IS NULL` respectively).
A `scripts/check_progress.py` SQL probe at the end of each chunk reports
remaining work; if any remains, a small follow-up job calls
`gh workflow run` to dispatch the next chunk.

Stage C ends with `scripts/check_regression.py`, which compares
current corpus counts against a watermark in `refresh_watermarks`
(see [`sql/0004_refresh_watermarks.sql`](../../sql/0004_refresh_watermarks.sql))
and fails the workflow if any count dropped >5% since the last
successful refresh. Workflow failures surface as GitHub's email alert.

## Alternatives considered

- **One mega-job per stage.** Doesn't fit. The 6-hour Actions limit is
  hard; a 16-hour README pass cannot be expressed as a single Actions
  job. Self-hosted runners would lift the limit but add maintenance
  surface that an entirely-free portfolio project shouldn't carry.
- **Offset-based chunking** (`--offset 0..50000`, `--offset
  50000..100000`, ...). Requires the orchestrator to know corpus
  size, balance chunk boundaries, and persist progress between runs.
  Time-bounded chunking sidesteps all three: the database is the
  source of truth for "what's left," each chunk re-queries it, and
  chunks naturally vary in size as work density changes (fewer rows
  remaining → faster chunks → fewer rechunks).
- **Cloud Run jobs / Cloud Scheduler.** Would work but introduces a
  second cloud platform. GitHub Actions is already used for the
  frontend deploy, the source is already on GitHub, and the free
  tier covers the workload (a full refresh is ~30 hours of runner
  time per week, well inside the 2000-minute limit on private repos
  and unlimited on public).
- **DigitalOcean droplet with cron.** $6/month, removes the chunking
  problem entirely (single 24-hour run is fine), and decouples
  refresh from CI. Plausible upgrade path. Rejected for now because
  it adds infrastructure (droplet provisioning, OS patching, cron
  monitoring) that GitHub Actions provides for free, and because
  treating the refresh as part of the project's CI surface keeps it
  visible — failed Actions runs show up next to failed test runs.
- **In-process orchestration** (one Python script that runs metadata
  → readme → index sequentially). Would need to run somewhere; same
  problem as above. The CLI entry points already exist as separate
  programs; chaining them at the workflow level instead of the
  process level keeps each stage's failure surface independent.

## Consequences

- ✅ The pipeline scales past the 6-hour Actions limit without
  introducing new infrastructure. A README pass that takes 16 hours
  runs as ~4 chunks; an index pass that takes 8 hours runs as ~2.
- ✅ Resumability is free. Both staged programs already filter on
  existing-row predicates because they were designed for crash safety
  (mid-run kill → restart picks up where it left off). The chunk
  boundary is the same boundary as the crash boundary; we got
  rechunking by accident the first time we made the pipeline
  crash-safe.
- ✅ The end-of-pipeline regression check catches silent corruption.
  If next week's run has 250K total repos but the watermark recorded
  280K, something deleted rows and the workflow fails with a clear
  message. Without this, a misconfigured `DELETE` or schema migration
  bug could quietly halve the corpus and the only signal would be
  someone noticing the search results got worse.
- ⚠️ **Self-rechunking requires a fine-grained PAT, not the
  auto-injected `GITHUB_TOKEN`.** GitHub deliberately blocks the
  default workflow token from triggering further `workflow_dispatch`
  events (anti-recursion measure). The rechunk job needs a separate
  `WORKFLOW_DISPATCH_PAT` repository secret with `actions: write`
  scope on this repo. This is the kind of footgun that's invisible
  on the first chunk and only surfaces when the second chunk fails
  to dispatch — easy to mis-debug as "the rechunk logic is broken"
  when the rechunk logic is fine and the token is wrong.
- ⚠️ Workers exit on a wall-clock deadline, not a queue-empty signal.
  In the indexer pipeline, this means an in-flight HTTP batch is
  *cancelled* mid-call when the deadline fires (workers are awaited
  by `asyncio.wait` and then cancelled in `main.py`). Idempotency
  saves us — the next chunk re-fetches the pending list from the DB
  and the cancelled batch's repos are still missing embeddings, so
  they get embedded next time — but it does mean each chunk wastes
  whatever work was in flight at the deadline. The README pass uses
  a different model (deadline checked between iterations inside the
  worker, in-flight requests finish naturally); the indexer was
  aligned to the same shape in
  [`indexer/pipeline/worker.py`](../../indexer/pipeline/worker.py)
  so cancellation cost is now bounded to one batch's worth of work,
  not arbitrary in-flight state.
- ⚠️ Watermark schema is a new migration (`sql/0004_refresh_watermarks.sql`),
  not a `CREATE TABLE IF NOT EXISTS` inside the regression script.
  Schema lives with schema; the script reads and writes rows.
  Operationally this means the migration must run before the first
  Stage C completes — otherwise the regression check fails on the
  first run. This is documented in the migration file and the
  deployment notes; missing it once is the kind of one-time setup
  failure that's easy to fix and unlikely to recur.
- ⚠️ Wall-clock latency between "data changed" and "search reflects
  it" is up to a week (one cron cycle plus chunk-pipeline duration).
  Acceptable for a search engine over a slow-changing corpus; would
  not be acceptable for a system where freshness mattered (news,
  prices, etc.). The ADR is therefore implicitly tied to the
  read-mostly nature of the corpus.

## What would change this decision

- **Corpus growth past ~5M repos.** At that scale the per-chunk
  durations climb to where even chunked workflows can't finish in a
  week. The DigitalOcean-droplet alternative becomes the right
  shape — single long-running cron, no chunk boundaries to manage.
- **GitHub raises the per-job time limit.** The chunking machinery
  exists only because of the 6-hour cap; a 24-hour cap would let
  this collapse back to one job per stage.
- **Multi-tenancy** (search over multiple corpora, each refreshed
  independently). The current single-watermark design assumes one
  refresh pipeline writing to one set of tables. Multiple corpora
  would need either per-corpus watermarks or a different consistency
  model (e.g., shadow tables + atomic swap).
- **Sub-day freshness requirement.** Would force a streaming or
  near-real-time architecture (webhooks from GitHub, incremental
  embedding, online index updates), which is a fundamentally
  different design.
