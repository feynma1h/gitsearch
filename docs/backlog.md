# Backlog

Open items as of 2026-08-08, each with the condition that would
activate it. Decisions and their evidence live in `decisions/`; this
file only tracks what is parked and why, so it should stay short.

## Live-usage telemetry (next up)

Log real queries and result clicks — privacy-light, no accounts.
Purpose: learn the real query-class mix (navigational vs category vs
task), grow organic qrels that no judge model can synthesize, and
ground every later retrieval decision in usage instead of the
synthetic 200-query eval. Every other item below becomes cheaper to
decide once this exists. No ADR yet — design when picked up.

## Corpus refresh: paused, and what restarting it takes

The three refresh workflows (ADR 0014) ran on their crons through July
2026 and are now disabled in the repository's Actions settings, so the
deployed corpus is a snapshot rather than a live index. Everything below
about freshness assumes they are running again.

Restarting is a settings toggle plus two checks:

1. **`python scripts/check_regression.py --rebaseline`, once, before
   re-enabling.** The stored watermark holds an all-label embedding
   count written before that check was scoped to the serving label
   (~733K vs ~244K). Left alone, the first run's health check reads the
   difference as a two-thirds collapse, exits non-zero, and fails the
   workflow — and because the watermark is only advanced on the healthy
   path, every later run fails the same way until it is rebaselined.
   Nothing is corrupted either way; the refresh itself has already
   committed by then.
2. **Confirm the secrets.** `DATABASE_URL`, `CRAWLER_GH_TOKEN`,
   `EMBEDDING_SERVICE_URL`, `WORKFLOW_DISPATCH_PAT`. The last one is the
   footgun ADR 0014 records: its absence is invisible on the first chunk
   and only surfaces when the second fails to dispatch.

## Incremental freshness: reconcile sweep + conditional README refresh (designed, not started)

The corpus is currently refreshed by re-running the whole pipeline. An
incremental metadata crawl already exists (ADR 0015: a watermark in
`crawl_state` adds `pushed:>=` to every star shard), but it keys on
pushes, and GitHub offers no queryable signal for star changes at all —
starring bumps no timestamp. Two consequences: star counts on un-pushed
repos go stale, and a repo that falls *below* the 200-star floor is
structurally invisible, because the crawler only ever asks for
`stars:>=200`. Its row lingers with a frozen count forever.

Plan — use the exact signal where one exists, a cheap sweep where none does:

1. **Weekly reconcile sweep** (`scripts/reconcile.py` — new; the
   enumeration and diff were prototyped in a local audit notebook that
   is not part of the repo). Enumerate
   every `stars:>=200` repo id from GitHub, bucketed by `created_at`
   (immutable, so it dodges the 1000-result search cap), and diff against
   the DB. Ids GitHub has that the DB lacks → crossed above the floor,
   insert. Ids the DB has that the enumeration lacks → verify with one
   batched `nodes()` lookup before acting, because search is eventually
   consistent and does drop results: null → deleted or private;
   `stars < 200` → genuinely dropped. Flag those with `below_floor_since`
   rather than deleting — the foreign keys cascade, so a delete destroys
   the repo's embedding and its paid-for LLM guide, and a repo
   oscillating around 200 would churn through re-fetch and re-embed every
   few weeks. GC rows that stay flagged 90+ days. The same enumeration
   carries `stargazerCount` for free, which fixes star staleness
   corpus-wide in the same pass.
2. **Weekly README refresh.** A README change is always a push (even
   web-UI edits create a commit), so `pushed_at > readme_fetched_at` is a
   sound filter rather than a heuristic. Send `If-None-Match` from a
   stored `readme_etag`; 304 responses don't count against the REST
   budget, so unchanged repos cost wall time only. Confirm that on the
   first run — the limiter already reads `X-RateLimit-Remaining`.
3. **Re-embed** only where `repository_embeddings.source_hash` mismatches.
   That machinery already exists; volume is just the changed READMEs.

Schema additions: `below_floor_since`, `first_crawled_at` (the upsert
must never touch it, so "what entered the corpus this week" becomes one
query), and `readme_etag`. Plus a `--refresh` mode on the README pass,
which today only ever fetches rows where `readme_fetched_at IS NULL`.

Measured 2026-08-07, so this starts from a full corpus and needs no
backfill: 266,985 repos, 244,396 of 244,399 non-archived READMEs
fetched, 0 embeddings pending (733,188 stored, ≈3 model variants).
Steady-state costs, each step run separately: reconcile enumeration
~30–60 min wall (~2,800 GraphQL points); the diff fetch ~40 min on the
first run (a ~17.7K backlog at ~20 points per `nodes(ids:100)` query, so
it will sit through a budget reset) and minutes thereafter; README
refresh ~38K conditional requests ≈ 30–60 min, drawing roughly an hour
of REST budget for the few thousand that actually changed; re-embed
minutes. `make crawl-incremental` stays at a few minutes, `make crawl`
~25 min.

Sizing note for whoever picks this up: a *full* README pass is ~49 hours
(244K non-archived repos at the 5,000 REST requests/hour ceiling), which
is why ADR 0014 carries a correction note against its original ~16-hour
figure. The conditional refresh above is what removes the need to ever
run one again.

## Supabase Micro → Small (trigger-based)

The enrichment working set (198MB terms table + GINs) exceeds
Micro's cache, so per-vocabulary cold queries page-fault: 1–3s on
novel vocabulary vs ~120ms once Postgres has it cached (README;
ADR 0020). The keep-warm pinger only keeps its own query path hot;
diverse real-user vocabulary hits the cold path.
Trigger: real-traffic p95 above ~2s over a representative week —
then upgrade for the larger shared_buffers (roughly $5–15/mo extra;
verify current Supabase pricing).

## Embedding-service cold start: ONNX / model-bake (evidence-gated)

The keep-warm pinger (ADR 0019) already hides the cold path for
nearly all visitors at $0 standing cost. Reopen only on evidence of
real users hitting cold starts. Scope when reopened: export
bge-small to ONNX and verify embedding parity against the 244K
stored vectors (drops the ~30s torch import to ~1–2s), and/or bake
the model into the image (~8s of the wake). min-instances (~$8–15/mo)
remains the spend-instead-of-work alternative.

## enrich-v2 label flip (parked indefinitely)

Declined 2026-08-07 (ADR 0020): +0.013 judged nDCG is below the
+0.03 bar, and the judge-independent canary suite regresses −0.024
on navigational queries (Doc2Query homogenization in the dense
lane). The qrels themselves were verified clean by the second-family
spot-judge, so the small gain is real — just not worth the
navigational cost. Reopen only if usage telemetry shows
category/semantic queries underperforming. Prerequisite work if
reopened: down-weight enrichment text in document construction (or
cap its share of short docs), re-embed under a new label, re-run the
decision-grade eval against the +0.03 bar and the canary suite.
