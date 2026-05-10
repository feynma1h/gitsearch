# gitsearch

**Find GitHub repos by what they do, not just by their name.**

[Live demo →](https://feynma1h.github.io/gitsearch/)

```
"fast http server in rust"   →   tokio-rs/axum, actix/actix-web, hyperium/hyper
"kubernetes package manager" →   helm/helm
"vector database for embeddings" →  qdrant/qdrant, weaviate/weaviate, pgvector/pgvector
```

Type a natural-language query, get back GitHub repos ranked by semantic
relevance, popularity, and recency. ~280,000 repos crawled, ~20,000
fully indexed with embeddings; queries return in ~30ms once the services
are warm (~15s on first request after idle, due to Cloud Run scale-to-zero).

## What this is

A small but complete production-shaped system. Three Python services
(crawler, indexer, search) that share a Postgres+pgvector database,
plus a static frontend, all deployable to free-tier cloud
infrastructure. Every non-trivial design decision is recorded as an
[Architecture Decision Record](docs/decisions/) with the alternatives
considered and the conditions under which the decision should be
revisited.

The components, each with its own README:

- [`crawler/`](crawler/) — async crawler that fetches ~280K repo
  metadata via GitHub GraphQL and READMEs via REST, into Postgres.
- [`indexer/`](indexer/) — FastAPI embedding service
  (`bge-small-en-v1.5`) + async pipeline that embeds repos and writes
  vectors to pgvector.
- [`search/`](search/) — FastAPI service that takes a query, embeds
  it, runs hybrid vector + metadata search, and returns ranked repos.
- [`frontend/`](frontend/) — single-file static UI (no build step).

The components are deliberately separable. They share one Postgres
database; they don't share Python imports.

## Things worth a closer look

- **[ADR 0013](docs/decisions/0013-hybrid-scoring-formula.md)** — the
  hybrid scoring formula combining semantic similarity, log-normalised
  star count, and exponential recency decay. Each component is
  normalised to `[0, 1]` so the weights are interpretable; the math
  lives in [`search/service/ranking.py`](search/service/ranking.py)
  (pure Python, fully unit-tested) and is mirrored in SQL in
  [`search/service/db.py`](search/service/db.py).

- **The over-fetch + re-rank pattern** for hybrid search. HNSW gives
  top-K by similarity alone; we over-fetch candidates, then re-rank
  by hybrid score in the same SQL statement. Documented in ADR 0013.

- **[`indexer/pipeline/document_builder.py`](indexer/pipeline/document_builder.py)** —
  the choice of *what text to feed the embedding model* turns out to
  be at least as important as the model itself. ADR 0008 explains why
  the metadata header goes before the README (truncation safety) and
  why this decision is documented as policy.

- **[ADR 0001](docs/decisions/0001-sharded-star-range-crawling.md)** —
  density-calibrated star-range sharding with the long history of how
  the band layout got there. Three production incidents shaped this
  ADR: the original 1000-result-cap discovery, the secondary
  rate-limit incident at 15 workers, and the structural undersizing
  of low-star bands that was only caught by an explicit population
  audit. Worth reading as a case study in how an ADR accumulates the
  hard-won lessons the code itself doesn't teach.

- **[`search/eval/`](search/eval/)** — an offline evaluation harness
  that measures Recall@K and NDCG@K against a labelled query set.
  Lets weight tuning be data-driven rather than vibes-driven. Read
  [its README](search/eval/README.md) for the design choices.

- **The unit tests focus on the parts where bugs are subtle and
  silent** — shard-boundary correctness in the crawler, document
  truncation in the indexer, scoring math in the search service.
  Things where a test failing means you would otherwise have shipped
  bad rankings without noticing.

## How the pieces fit

```
              ┌───────────────────┐
              │   GitHub API      │
              │   GraphQL + REST  │
              └─────────┬─────────┘
                        │
                ┌───────▼────────┐
                │    crawler     │   metadata crawl: ~25 min local
                │   (batch job)  │   readme pass: ~16 hr / 280K repos
                └───────┬────────┘
                        │ writes
                        ▼
              ┌───────────────────┐
              │     Postgres      │
              │  repositories     │
              │  repository_      │
              │    embeddings     │
              └────┬─────────▲────┘
       reads ─────┘         │ writes
                             │
              ┌──────────────┴────┐
              │  indexer pipeline │   batch job; one pass for the
              │   (batch job)     │   corpus, then resumes only repos
              └──────┬────────────┘   that still need embedding
                     │ uses
                     ▼
              ┌───────────────────┐         ┌───────────────────┐
              │ embedding service │ ◀────── │  search service   │
              │ POST /embed       │  embed  │ POST /search      │ ◀── user
              │ (long-lived)      │  query  │ (long-lived)      │
              └───────────────────┘         └─────────┬─────────┘
                                                      │ reads
                                                      ▼
                                              (pgvector + HNSW)
```

There's one schema migrated in four steps (`sql/0001..0004`) and three
Python services that don't share code — each can be deployed, tested,
and reasoned about independently. Cross-component coupling is through
Postgres and HTTP, both of which are easy to inspect.

## End-to-end run (compose)

```bash
# 1. Configure
cp .env.example .env
# Fill in GITHUB_TOKEN. The other defaults work for local dev.

# 2. Bring up Postgres, embedding service, search service.
make up                   # docker compose up -d
make migrate              # apply sql/0001..0004

# 3. Populate the corpus. Batch jobs that run from the host.
make install              # one-time: pip install for all components
make crawl                # ~25 min for 280K repos (5000 GraphQL pts/hr)
make readmes              # ~16 hr for 280K repos (REST is rate-bound)
make index                # ~8 hr for 280K repos (CPU embedding)
make build-hnsw           # one-time, AFTER index finishes (ADR 0011)

# 4. Search.
curl -s localhost:8002/search \
    -H 'Content-Type: application/json' \
    -d '{"query": "fast http server in rust", "filters": {"language": "Rust"}}' | jq

# 5. Optional: measure search quality.
make eval                 # runs search/eval/queries.json against the live service
```

If you want to run the long-lived services on the host instead of in
compose (typical during development of `search/` or `indexer/`):

```bash
make serve-embed          # in one terminal
make serve-search         # in another
```

## End-to-end run (no docker)

```bash
# Bring your own Postgres 16 with pgvector (Supabase free tier works).
export DATABASE_URL=postgresql://...
export GITHUB_TOKEN=ghp_...

make install
make migrate
make serve-embed &        # background
make crawl
make readmes
make index
make build-hnsw
make serve-search &
make eval                 # sanity check
```

## Deployed system

The live demo runs on free-tier infrastructure. End-to-end:

| Component               | Where it runs                            | Why there                                                                  |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------- |
| Postgres + pgvector     | Supabase (Pro, ap-southeast-1)           | Managed pgvector with PITR; cheaper and lower-effort than self-hosting RDS |
| Embedding service       | Google Cloud Run (asia-southeast1, 2 GiB)| Scale-to-zero between requests; ~15s cold start                            |
| Search service          | Google Cloud Run (asia-southeast1, 512 MiB)| Same; tuned for embedding-service cold-start tolerance                   |
| Frontend                | GitHub Pages                             | Static; deploys via `.github/workflows/deploy-frontend.yml`                |
| Weekly corpus refresh   | GitHub Actions                           | Three chained workflows; see below                                         |

**Weekly corpus refresh** runs as three chained GitHub Actions
workflows in [`.github/workflows/`](.github/workflows/):

1. `refresh-metadata.yml` — runs the metadata crawl. Single ~5-hour
   job (Cloud-CI egress IPs are throttled hard by GitHub's secondary
   rate limit; see ADR 0001's "CI-IP throttling" consequence).
2. `refresh-readme.yml` — fetches READMEs in 5-hour chunks. The job
   self-rechunks via `gh workflow run` until a SQL probe reports no
   remaining work. Resumability is free — `readme_pass.py` only
   selects rows where `readme_fetched_at IS NULL`.
3. `refresh-index.yml` — embeds new repos in 5-hour chunks. Same
   self-rechunk pattern. Final job runs a regression check against
   `refresh_watermarks` and fails the workflow if any corpus count
   dropped >5% since the last successful refresh.

The full design (chunking strategy, watermark-based regression check,
the `WORKFLOW_DISPATCH_PAT` requirement for self-rechunking) is in
[ADR 0014](docs/decisions/0014-chunked-actions-refresh.md).

**Cost.** Approximately $30/month: Supabase Pro ($25) plus a single
Cloud Run min-instance on the search service ($5) to eliminate
cold-start latency. The embedding service stays scale-to-zero — it's
called from the search service's hot path, but only on cache misses,
and the Cloud Run cold-start delay is acceptable given indexing only
happens weekly.

## Project layout

```
gitsearch/
├── README.md              ← this file
├── docker-compose.yml     ← postgres + embedding + search
├── Makefile               ← common tasks; see `make help`
├── .env.example
│
├── .github/workflows/     ← deploy-frontend + 3-stage refresh pipeline
│
├── docs/
│   └── decisions/         ← all ADRs (0001..0014); one decision per file
│
├── sql/                   ← migrations applied in numeric order
│   ├── 0001_initial_schema.sql
│   ├── 0002_readme_columns.sql
│   ├── 0003_repository_embeddings.sql
│   └── 0004_refresh_watermarks.sql
│
├── scripts/               ← operational scripts (progress probes, regression checks)
│
├── crawler/               ← async crawler, GraphQL + REST -> Postgres
├── indexer/               ← embedding service + indexing pipeline
├── search/                ← search API + eval harness
└── frontend/              ← static HTML/JS UI for the search API
```

Each component README is the canonical source for that component's
details — flags, environment variables, internal architecture, known
limitations.

## Design decisions

This project documents its design decisions as Architecture Decision
Records (ADRs) in [`docs/decisions/`](docs/decisions/). Every
non-trivial choice — sharded crawling strategy, separate embeddings
table, HTTP over gRPC, HNSW over IVFFlat, hybrid scoring formula,
chunked CI refresh — has an ADR explaining what was chosen, what was
rejected, and the conditions under which the decision should be
revisited.

The format is uniform and append-only:
[`docs/decisions/README.md`](docs/decisions/README.md) explains it and
indexes the current 14 ADRs.

If you're considering a change to anything substantive in this codebase,
**read the relevant ADR first**. The "What would change this decision"
section in each ADR is the project's collective memory of *when* a
choice should be reconsidered. If your change matches a trigger
condition there, write the next ADR; if it doesn't, the existing
choice probably has a reason you're about to rediscover the hard way.

## Tests

```bash
make install-dev
make test
```

The unit tests focus on the parts where bugs are subtle and silent:
the crawler's shard-boundary math and rate limiter (including the
density-calibrated band layout), the indexer's document-construction
format and worker-deadline behaviour, the search service's hybrid
scoring. Integration tests against a live Postgres aren't in the
unit-test set; the eval harness covers end-to-end search quality.

## Known gaps

These are real limitations, not things I forgot to do. Each is
discussed (or flagged for future discussion) in the ADRs:

- **Single token, single instance.** The crawler runs on one GitHub
  token; the embedding service is one CPU process; the search service
  is one replica. Horizontal scale would be straightforward but isn't
  needed for the current corpus size. See ADRs 0001, 0007, 0010.
- **CI-IP throttling.** The crawler that runs at 5 workers in ~25 min
  locally needs ~5 hours from a GitHub Actions runner because Azure
  egress IPs are flagged aggressively by GitHub's secondary rate
  limit. Documented in ADR 0001; the only real fix is a self-hosted
  runner on a residential IP, which is more setup than the project
  warrants.
- **One embedding model at a time.** ADR 0006 enables A/B at the storage
  layer (the embeddings table is keyed by `model_name`), but the
  indexer pipeline and search service each currently use one. A
  `?model=` parameter on `/search` is a small change once a second
  model is indexed.
- **No BM25 / lexical lane.** Pure dense retrieval struggles with
  exact-name lookups and rare tokens. Combining lexical + semantic via
  RRF is the documented next step. See ADR 0013.
- **No evaluation harness usage in CI.** `search/eval/` exists and can
  be run on demand, but ranking regressions aren't caught automatically
  on PRs. The harness is small enough to wire into CI when ranking
  quality starts mattering for review velocity.
- **The 384-dim hardcoding.** The schema has `vector(384)`. Swapping to
  a different-dim model means a separate table or a migration.
  Mentioned in ADR 0007 and the indexer README; doesn't have its own
  ADR yet because the right resolution depends on which model we
  switch to.
