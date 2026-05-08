# gitsearch

Semantic search over GitHub repositories. Type a natural-language
query like *"fast http server in rust"* and get back ranked repos
based on what each repo *is*, not just keyword overlap on the name.

Three components, each in its own subdirectory with its own README:

- [`crawler/`](crawler/) — async crawler that fetches ~100K repo
  metadata via GraphQL and READMEs via REST, into Postgres.
- [`indexer/`](indexer/) — FastAPI embedding service (bge-small-en-v1.5)
  + async pipeline that embeds repos and writes vectors to pgvector.
- [`search/`](search/) — FastAPI service that takes a query, embeds it,
  runs hybrid vector + metadata search, and returns ranked repos.

The components are deliberately separable. They share one Postgres
database; they don't share Python imports.

## How the pieces fit

```
              ┌───────────────────┐
              │   GitHub API      │
              │   GraphQL + REST  │
              └─────────┬─────────┘
                        │
                ┌───────▼────────┐
                │    crawler     │   metadata crawl: ~4 min
                │   (batch job)  │   readme pass: ~4 hr / 20K repos
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

There's one schema migrated in three steps (`sql/0001`, `0002`, `0003`)
and three Python services that don't share code — each can be deployed,
tested, and reasoned about independently. Cross-component coupling is
through Postgres and HTTP, both of which are easy to inspect.

## End-to-end run (compose)

```bash
# 1. Configure
cp .env.example .env
# Fill in GITHUB_TOKEN. The other defaults work for local dev.

# 2. Bring up Postgres, embedding service, search service.
make up                   # docker compose up -d
make migrate              # apply sql/0001..0003

# 3. Populate the corpus. Batch jobs that run from the host.
make install              # one-time: pip install for all components
make crawl                # ~4 min for 100K repos (5000 GraphQL pts/hr)
make readmes              # ~4 hr for 20K repos (REST is rate-bound)
make index                # ~30 min for 20K repos (CPU embedding)
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

## Project layout

```
gitsearch/
├── README.md              ← this file
├── docker-compose.yml     ← postgres + embedding + search
├── Makefile               ← common tasks; see `make help`
├── .env.example
│
├── docs/
│   └── decisions/         ← all ADRs (0001..0012); one decision per file
│
├── sql/                   ← migrations applied in numeric order
│   ├── 0001_initial_schema.sql
│   ├── 0002_readme_columns.sql
│   └── 0003_repository_embeddings.sql
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
table, HTTP over gRPC, HNSW over IVFFlat, hybrid scoring formula — has
an ADR explaining what was chosen, what was rejected, and the
conditions under which the decision should be revisited.

The format is uniform and append-only:
[`docs/decisions/README.md`](docs/decisions/README.md) explains it and
indexes the current 12 ADRs.

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
the crawler's shard-boundary math and rate limiter, the indexer's
document-construction format, the search service's hybrid scoring.
Integration tests against a live Postgres aren't in the unit-test
set; the eval harness covers end-to-end search quality.

## Known gaps

These are real limitations, not things I forgot to do. Each is
discussed (or flagged for future discussion) in the ADRs:

- **Single token, single instance.** The crawler runs on one GitHub
  token; the embedding service is one CPU process; the search service
  is one replica. Horizontal scale would be straightforward but isn't
  needed for the 100K-repo target. See ADRs 0001, 0007, 0010.
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
- **Asymmetry in resumability.** The metadata crawl is non-resumable
  (relies on `ON CONFLICT DO UPDATE` to make a re-run safe but
  wasteful); the README pass and indexer pipeline both resume from
  where they left off. The asymmetry is deliberate but not yet
  documented as an ADR.
