# Search

FastAPI service that takes a natural-language query and returns the
top-N most relevant repos from the indexed corpus. Retrieval runs three
lanes — full-text, dense vectors, and name matching — fused by
Reciprocal Rank Fusion, then ranked by an additive blend of relevance,
saturated popularity, and recency.

This is the front door of the system. It does not own any data — it
reads from the same Postgres the crawler and indexer wrote to, and
calls the embedding service from the indexer to embed the user's query
at request time.

## How it works

```
                         POST /search { "query": "..." }
                                       │
                                       ▼
                              ┌─────────────────┐
                              │  search service │
                              │   (FastAPI)     │
                              └────────┬────────┘
                                       │
                       ┌───────────────┼────────────────┐
                       │                                │
                       ▼                                ▼
              ┌─────────────────┐            ┌──────────────────────────┐
              │ embedding svc   │            │ Postgres (one statement) │
              │ POST /embed     │            │  ├─ full-text lane       │
              │ (1-text batch)  │            │  ├─ dense (halfvec HNSW) │
              └────────┬────────┘            │  └─ name (pg_trgm)      │
                       │                     └──────────┬───────────────┘
                       └────────────┬───────────────────┘
                                    ▼
                        ┌────────────────────────┐
                        │ weighted RRF fusion    │
                        │ + additive blend       │  relevance + sat(stars)
                        │ + exact-name-first     │  + recency, demotions
                        └────────────────────────┘
```

A request goes through three stages:

1. **Embed the query.** The query is sent as a 1-element batch to the
   embedding service. bge-small-en-v1.5 is symmetric, so no `query: `
   prefix — embedding the raw text is correct.
2. **Three retrieval lanes, one SQL statement**, all seeing the same
   WHERE filters (language, topics, min stars, archived):
   - *Full-text* — matches content terms in name/topics/language/
     description (and full websearch matches anywhere, README
     included), ordered by how many query terms a repo covers, then by
     stars within each coverage tier.
   - *Dense* — pgvector KNN over a half-precision HNSW expression
     index; iterative scans keep filtered searches from running short.
   - *Name* — pg_trgm exact / prefix / fuzzy matching on the repo
     name, so typos ("pytorhc") still land.
3. **Fusion + blend.** Lanes merge via weighted RRF (`w/(k + rank)`),
   then the final order is
   `relevance + w_pop·sat(stars) + w_rec·recency`, where `sat(x) =
   x/(x + pivot)` saturates popularity (megastars can't drown
   relevance) and recency has a floor (finished classics don't sink).
   A query that exactly matches a repo's name sorts that repo first,
   popularity-independent. Archived repos and forks are demoted.
   See [ADR 0018](../docs/decisions/0018-three-lane-hybrid-retrieval.md).

## Setup

This assumes the crawler has populated `repositories`, the indexer has
populated `repository_embeddings`, and the indexes have been built
(`make migrate`, then `make build-hnsw` and `make build-hnsw-halfvec`).

```bash
# 1. Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Env
export DATABASE_URL=postgresql://...
export EMBEDDING_SERVICE_URL=http://localhost:8001  # default
export ANTHROPIC_API_KEY=sk-ant-...                 # optional; enables /guide
export GITHUB_TOKEN=github_pat_...                  # optional; /guide explores the live repo

# 3. Run
uvicorn service.server:app --host 0.0.0.0 --port 8002
```

The embedding service must already be running (see `indexer/README.md`).

## Endpoints

```
GET  /health
POST /search   {
                 "query":   "fast http server",   # required
                 "limit":   10,                   # 1..100, default 10
                 "filters": {                     # all optional
                   "language":         "Rust",
                   "topics":           ["http", "web"],
                   "min_stars":        500,
                   "exclude_archived": true       # default true
                 },
                 "weights": {                     # all optional
                   "similarity":     1.0,         # weight of fused relevance
                   "stars":          0.3,
                   "recency":        0.2,
                   "half_life_days": 365,
                   "full_text_weight": 1.0,       # RRF lane weights + k —
                   "semantic_weight":  1.0,       # exposed for eval sweeps;
                   "name_weight":      0.5,       # normal clients omit them
                   "rrf_k":            50
                 }
               }
               → { "hits":  [...], "model": "...", "took_ms": N }
```

Each hit returns: `repo_id`, `full_name`, `description`, `url`,
`primary_language`, `topics`, `stars`, `pushed_at`, `similarity`
(0..1, raw cosine — 0.0 for repos without an embedding, which the
lexical lanes can still surface), `exact_name` (query is exactly this
repo's name), `hybrid_score` (the ranking key), and the three
`*_contribution` fields (relevance / stars / recency shares that sum
to `hybrid_score` — what the frontend's "why this rank?" bar draws).

```
GET  /guide/{repo_id}
     → { "repo_id": "...", "full_name": "...",
         "guide": "## What it is\n...",   # GFM, fixed five-section format
         "model": "...", "cached": true|false }
```

Returns a short "how do I use this?" guide for one repo, generated once
and cached (see [ADR 0016](../docs/decisions/0016-llm-usage-guide.md)).
With `GITHUB_TOKEN` set, the model explores the live repository while
writing — listing the file tree and reading manifests, docs, and examples
through a bounded tool loop — so install/run steps come from the real
files, not just the README
([ADR 0017](../docs/decisions/0017-agentic-guide-generation.md)); without
the token it falls back to the stored README alone. Requires
`ANTHROPIC_API_KEY`; without it the endpoint returns 503 and search is
unaffected.

### Examples

Pure relevance — disable popularity and recency:

```bash
curl -s -X POST http://localhost:8002/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "build tool for monorepos",
    "weights": {"stars": 0, "recency": 0}
  }' | jq '.hits[] | {full_name, similarity, hybrid_score}'
```

"What's popular and recent in Rust web frameworks":

```bash
curl -s -X POST http://localhost:8002/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "web framework",
    "filters": {"language": "Rust", "min_stars": 1000},
    "weights": {"recency": 0.5, "half_life_days": 180}
  }' | jq '.hits[] | {full_name, stars, pushed_at, hybrid_score}'
```

## Configuration

Layered, same convention as the crawler and indexer:

| Source              | Purpose                                              |
| ------------------- | ---------------------------------------------------- |
| `service/config.py` | Defaults referenced from multiple files (lane depths, RRF, saturation pivot clamp, recency floor, demotions). |
| Env vars            | `DATABASE_URL`, `EMBEDDING_SERVICE_URL`, `SEARCH_RATE_LIMIT` (raise only for local eval sweeps). |
| Request body        | `weights`, `filters`, `limit` per call.              |

The model name is in `config.py` and **must match** what the indexer
wrote and what the embedding service is serving. If they drift, the
JOIN on `model_name` matches nothing and you get zero results — silent.
A startup health-probe against the embedding service that asserts model
parity is a reasonable next addition.

Usage guides (`/guide`) add two more knobs: `ANTHROPIC_API_KEY` (enables
the endpoint) and `GITHUB_TOKEN` (enables full-repo exploration), plus the
`GUIDE_*` defaults in `config.py` (model, output length, README
truncation, rate limit, and the exploration bounds — tool rounds, listing
size, per-file size). See
[ADR 0016](../docs/decisions/0016-llm-usage-guide.md) and
[ADR 0017](../docs/decisions/0017-agentic-guide-generation.md).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests focus on `ranking.py` (the fusion/blend math) and
`eval/metrics.py` (the numbers phase gates are decided on — including a
parity check against ir_measures/trec_eval where installed). The SQL is
left to integration tests against a live Postgres; that's not in the
unit-test set.

## Evaluation

[`eval/`](eval/) is a decision-grade harness: a 200-query stratified
set, a hand-curated canary suite with verified canonical answers, an
UMBRELA LLM judge producing pooled graded qrels, and an A/B comparator
with a paired significance test. `make eval` still runs the quick
5-query regression check; see [`eval/README.md`](eval/README.md) for
the full workflow.

## Design decisions

Architectural choices live in [`../docs/decisions/`](../docs/decisions/) as
ADRs, continuing the project's contiguous numbering:

- [ADR 0012 — Search as a separate service](../docs/decisions/0012-search-as-a-separate-service.md)
- [ADR 0013 — Hybrid scoring formula and over-fetch + re-rank](../docs/decisions/0013-hybrid-scoring-formula.md) (superseded)
- [ADR 0016 — LLM-generated repository usage guides](../docs/decisions/0016-llm-usage-guide.md)
- [ADR 0017 — Agentic full-repo exploration for usage guides](../docs/decisions/0017-agentic-guide-generation.md)
- [ADR 0018 — Three-lane hybrid retrieval with RRF fusion and an additive popularity blend](../docs/decisions/0018-three-lane-hybrid-retrieval.md)

The most consequential files for ranking quality are
[`service/ranking.py`](service/ranking.py) (the canonical formula) and
the companion SQL in [`service/db.py`](service/db.py), which implements
the same math in Postgres and must stay in sync with it.

## Known limitations

- **No query caching.** Common queries ("react", "kubernetes") embed
  and search every time. An in-memory LRU on the embedding step would
  cut latency on hot queries (the embed step dominates when warm).
  Held off as feature creep until measured QPS justifies it.
- **Lexical scoring is coverage-based, not BM25.** Postgres FTS has no
  IDF. On this corpus's short, uniform documents that is second-order
  (measured before shipping ADR 0018), but if eval ever shows lexical
  scoring is the bottleneck, a bm25s/SQLite-FTS5 sidecar is the
  recorded escape hatch.
- **Single embedding model.** ADR 0006 makes A/B testing models
  trivial at the storage layer, but the search service today only
  reads embeddings for one model at a time. A `?model=` request
  param is a small change once a second model is indexed.
- **Eval harness not wired into CI.** It runs on demand; wiring it
  into CI is the next step once ranking quality starts gating review.
