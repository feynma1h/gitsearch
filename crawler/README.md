# GitHub Crawler

Async crawler that fetches GitHub repository metadata via the GraphQL Search
API and persists it to Postgres. Designed as the ingestion layer for a
larger semantic-search project.

Crawls roughly **280K repositories in ~25 minutes** on a single GitHub token,
limited primarily by the 5000 points/hour GraphQL budget.

## How it works

GitHub's search API caps any single query at 1000 results, which means a
naive "all repos sorted by stars" query is impossible. The crawler works
around this by **sharding** the search space into non-overlapping star-range
queries and processing them concurrently.

```
                       ┌────────────────────┐
                       │  shard generator   │  stars:200..200, stars:201..201, ...
                       └─────────┬──────────┘
                                 │
                       ┌─────────▼──────────┐
                       │     asyncio.Queue  │
                       └─────────┬──────────┘
                                 │
        ┌────────────────┬───────┴────────┬────────────────┐
        ▼                ▼                ▼                ▼
   ┌─────────┐      ┌─────────┐      ┌─────────┐      ┌─────────┐
   │ worker  │ ...  │ worker  │ ...  │ worker  │ ...  │ worker  │
   └────┬────┘      └────┬────┘      └────┬────┘      └────┬────┘
        │                │                │                │
        └────────────────┴───────┬────────┴────────────────┘
                                 │
                  ┌──────────────▼──────────────┐
                  │  shared GitHubRateLimiter   │
                  │  (asyncio.Lock-protected)   │
                  └──────────────┬──────────────┘
                                 │
                       ┌─────────▼──────────┐
                       │  Postgres (asyncpg)│
                       │  upsert by id      │
                       └────────────────────┘
```

Workers pull shards from a shared queue, paginate through each shard with
GraphQL cursors, and upsert results into Postgres. A shared rate limiter
coordinates request pacing using GitHub's own `rateLimit` field as the
source of truth.

### Sharding strategy

Star distribution is heavily skewed: there are millions of repos with under
10 stars and only thousands with over 10K. Uniform-width shards either miss
data in the dense tail or waste queries in the sparse head, so shards use
variable widths:

| Star range       | Shard width |
| ---------------- | ----------- |
| 200 – 400        | 1           |
| 400 – 1,000      | 2           |
| 1,000 – 2,000    | 50          |
| 2,000 – 5,000    | 100         |
| 5,000 – 10,000   | 500         |
| 10,000 – 50,000  | 5,000       |
| 50,000+          | open-ended  |

GitHub's `stars:A..B` syntax is **inclusive on both ends**, so adjacent
shards must not share endpoints — the generator emits `stars:1000..1049`,
`stars:1050..1099`, etc., not `stars:1000..1050`, `stars:1050..1100`.

## Setup

```bash
# 1. Postgres (any 14+ instance works; Supabase free tier is fine)
psql "$DATABASE_URL" -f ../sql/0001_initial_schema.sql
psql "$DATABASE_URL" -f ../sql/0002_readme_columns.sql

# 2. Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Env (the .env.example is at the project root in the monorepo).
cp ../.env.example ../.env
# Fill in GITHUB_TOKEN and DATABASE_URL.

# 4. Run the metadata crawl (~25 minutes for 280K repos).
set -a; source .env; set +a
python -m src.main

# 5. Fetch READMEs for the top 20K repos (~4 hours on one token).
python -m src.readme_pass --top-n 20000
```

## CLI: metadata crawler (`src.main`)

```
python -m src.main [--workers N] [--min-stars N] [--deadline-seconds N] [--log-level LEVEL]
```

| Flag                  | Default | Description                                       |
| --------------------- | ------- | ------------------------------------------------- |
| `--workers`           | 5       | Concurrent workers. 5 is a safe default for one token; higher trips GitHub's secondary rate limit (see [ADR 0001](../docs/decisions/0001-sharded-star-range-crawling.md)). |
| `--min-stars`         | 200     | Lower bound on stars. At ~200 the population is ~280K repos today (it has grown well past the original ~100K target); see [ADR 0003](../docs/decisions/0003-min-stars-threshold.md). |
| `--deadline-seconds`  | none    | Optional wall-clock cap; workers exit cleanly past this. |
| `--log-level`         | INFO    | DEBUG / INFO / WARNING / ERROR                    |

## CLI: README pass (`src.readme_pass`)

The README pass runs *after* the metadata crawl and uses GitHub's REST API
(separate rate-limit budget from GraphQL). It is resumable — only repos
where `readme_fetched_at IS NULL` are processed, ordered by stars desc.

```
python -m src.readme_pass [--workers N] [--top-n N] [--deadline-seconds N] [--log-level LEVEL]
```

| Flag                  | Default | Description                                       |
| --------------------- | ------- | ------------------------------------------------- |
| `--workers`           | 30      | Concurrent workers. REST is cheaper per-request than GraphQL. |
| `--top-n`             | 20000   | Max repos per run. 20K is a "demo-quality" target; raise for full coverage. |
| `--deadline-seconds`  | none    | Optional wall-clock cap.                          |
| `--log-level`         | INFO    | DEBUG / INFO / WARNING / ERROR                    |

A single token can fetch ~5000 READMEs/hour, so the top 20K takes ~4 hours and
the full ~280K corpus takes ~16 hours. Crashes are safe — restart picks up where
it left off.

## Schema

See [`../sql/0001_initial_schema.sql`](../sql/0001_initial_schema.sql) and
[`../sql/0002_readme_columns.sql`](../sql/0002_readme_columns.sql). The metadata
crawl populates everything except `readme`, `readme_status`, and
`readme_fetched_at`; the README pass fills those in.

`readme_status` is informational: `ok` (fetched), `not_found` (repo has no
README), `empty` (file exists but blank), `error` (transient failure —
clear `readme_fetched_at` to retry).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the shard generator (boundary correctness) and the rate limiter
(concurrency safety) — the two pieces where bugs are subtle and silent.

## Design decisions

Significant architectural choices are documented in
[`../docs/decisions/`](../docs/decisions/) as Architecture Decision Records
(ADRs). Each ADR captures the context, the decision, alternatives that
were rejected, and what would change the decision later. They are
append-only — when a decision is revised, a new ADR supersedes the old
one rather than rewriting it.

## Known limitations

- **Single-token only.** Multi-token rotation would let us push past the
  5000 points/hour ceiling, but isn't needed for the ~280K-repo metadata
  crawl. The README pass is more rate-limit-bound — full README coverage of
  the whole corpus would benefit from rotation.
- **No resume for the metadata crawl.** A crash mid-crawl re-processes
  shards from scratch on the next run. The `ON CONFLICT DO UPDATE` makes
  this safe but wasteful. The README pass *does* support resume.
