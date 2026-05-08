# Search

FastAPI service that takes a natural-language query and returns the
top-N most relevant repos from the indexed corpus, ranked by a hybrid
score combining semantic similarity, popularity, and recency.

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
              ┌─────────────────┐            ┌──────────────────┐
              │ embedding svc   │            │   Postgres       │
              │ POST /embed     │            │   pgvector +     │
              │ (1-text batch)  │            │   HNSW index     │
              └────────┬────────┘            └──────────┬───────┘
                       │                                │
                       └────────────┬───────────────────┘
                                    ▼
                        ┌────────────────────┐
                        │ overfetch top-K    │  e.g. K=50
                        │ by similarity      │
                        └─────────┬──────────┘
                                  │
                                  ▼
                        ┌────────────────────┐
                        │ re-rank by hybrid  │  sim + stars + recency
                        │ score, return top-N│  default N=10
                        └────────────────────┘
```

A request goes through three stages:

1. **Embed the query.** The query is sent as a 1-element batch to the
   embedding service. bge-small-en-v1.5 is symmetric, so no `query: `
   prefix — embedding the raw text is correct.
2. **Vector search with filters.** A pgvector HNSW lookup returns
   candidates ordered by cosine similarity. WHERE filters (language,
   topics, min stars, archived) are applied during the scan.
3. **Hybrid re-rank.** Each candidate gets a hybrid score combining
   normalized similarity, normalized log-stars, and exponential recency
   decay. The candidates are sorted by hybrid score; the top N are
   returned. See [ADR 0013](../docs/decisions/0013-hybrid-scoring-formula.md).

All three stages happen in one SQL statement after the embed call —
the candidate fetch and the re-rank are a single CTE.

### Why over-fetch?

HNSW gives top-K **by similarity alone**. If `weights.stars > 0`, the
top-N by hybrid score may include items that ranked, say, 23rd by
similarity but are popular enough to win on hybrid. Without
over-fetching, those items are invisible. The service over-fetches
`max(50, 5 * limit)` candidates (capped at 500) so the re-ranker has
room to surface them. Documented in
[ADR 0013](../docs/decisions/0013-hybrid-scoring-formula.md).

## Setup

This assumes the crawler has populated `repositories`, the indexer has
populated `repository_embeddings`, and the HNSW index has been built.

```bash
# 1. Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Env
export DATABASE_URL=postgresql://...
export EMBEDDING_SERVICE_URL=http://localhost:8001  # default

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
                   "similarity":     1.0,
                   "stars":          0.3,
                   "recency":        0.2,
                   "half_life_days": 365
                 }
               }
               → { "hits":  [...], "model": "...", "took_ms": N }
```

Each hit returns: `repo_id`, `full_name`, `description`, `url`,
`primary_language`, `topics`, `stars`, `pushed_at`, `similarity`
(0..1, raw cosine), and `hybrid_score` (the actual ranking key).

### Examples

Pure semantic search — disable popularity and recency:

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
| `service/config.py` | Defaults referenced from multiple files.             |
| Env vars            | `DATABASE_URL`, `EMBEDDING_SERVICE_URL`.             |
| Request body        | `weights`, `filters`, `limit` per call.              |

The model name is in `config.py` and **must match** what the indexer
wrote and what the embedding service is serving. If they drift, the
JOIN on `model_name` matches nothing and you get zero results — silent.
A startup health-probe against the embedding service that asserts model
parity is a reasonable next addition.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests focus on `ranking.py` — the scoring math is the part where bugs
are subtle and silent (an off-by-one denominator quietly destroys
ranking quality without raising). The SQL is left to integration tests
against a live Postgres; that's not in the unit-test set.

## Design decisions

Architectural choices live in [`../docs/decisions/`](../docs/decisions/) as
ADRs, continuing the project's contiguous numbering:

- [ADR 0012 — Search as a separate service](../docs/decisions/0012-search-as-a-separate-service.md)
- [ADR 0013 — Hybrid scoring formula and over-fetch + re-rank](../docs/decisions/0013-hybrid-scoring-formula.md)

The most consequential file in the search service for ranking quality
is [`service/ranking.py`](service/ranking.py) — changes there directly
affect every result. The companion SQL in
[`service/db.py`](service/db.py) implements the same formula in
Postgres and must stay in sync.

## Known limitations

- **No lexical / BM25 lane.** Pure semantic search is bad at exact-name
  matches ("redis" the project vs. "redis" the concept) and at
  uncommon terms the embedder hasn't seen well. A BM25 lane combined
  via RRF is the standard next step. Worth real measurement before
  adding — the failure mode might already be tolerable.
- **No query caching.** Common queries ("react", "kubernetes") embed
  and search every time. An in-memory LRU on the embedding step would
  cut latency on hot queries by ~80% (the embed step dominates). Held
  off as feature creep until measured QPS justifies it.
- **No evaluation harness yet.** Weight tuning currently is vibes.
  See [`eval/README.md`](eval/README.md) for the proposed approach;
  the harness is a recommended next addition before any further weight
  tuning.
- **Single embedding model.** ADR 0006 makes A/B testing models
  trivial at the storage layer, but the search service today only
  reads embeddings for one model at a time. A `?model=` request
  param is a small change once a second model is indexed.
- **Filter selectivity vs. HNSW.** Very narrow filters
  (`language=Cobol`) may return fewer rows than `limit` because HNSW
  applies the filter post-traversal. The over-fetch and bumped
  `ef_search` mitigate this; pgvector 0.8's iterative scans are the
  fix if it becomes a real problem.
