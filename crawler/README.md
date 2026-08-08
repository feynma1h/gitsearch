# GitHub Crawler

Async crawler that fetches GitHub repository metadata via the GraphQL Search
API and persists it to Postgres. Designed as the ingestion layer for a
larger semantic-search project.

Crawls roughly **267K repositories in ~25 minutes** on a single GitHub token,
limited primarily by the 5000 points/hour GraphQL budget.

Four passes, run in this order, each its own program:

| Pass                 | Writes                              | When                       |
| -------------------- | ----------------------------------- | -------------------------- |
| `src.main`           | `repositories` (metadata)           | once, then incrementally   |
| `src.readme_pass`    | `repositories.readme` + status      | after the crawl, resumable |
| `src.mine_awesome`   | `repository_enrichment`             | optional, idempotent       |
| `src.deps_dev_pass`  | `repository_signals`                | optional, idempotent       |

The first two are the corpus; the last two are retrieval side-tables that
search degrades gracefully without.

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

# 4. Run the metadata crawl (~25 minutes for ~267K repos).
set -a; source ../.env; set +a
python -m src.main

# 5. Fetch READMEs for the top 20K repos (~4 hours on one token).
python -m src.readme_pass --top-n 20000
```

## CLI: metadata crawler (`src.main`)

```
python -m src.main [--workers N] [--min-stars N] [--deadline-seconds N]
                   [--incremental] [--since ISO8601] [--log-level LEVEL]
```

| Flag                  | Default | Description                                       |
| --------------------- | ------- | ------------------------------------------------- |
| `--workers`           | 5       | Concurrent workers. 5 is a safe default for one token on a developer machine; higher trips GitHub's secondary rate limit. CI runs `--workers 2` — Actions runners' shared egress IPs hit that limit sooner (see [ADR 0001](../docs/decisions/0001-sharded-star-range-crawling.md)). |
| `--min-stars`         | 200     | Lower bound on stars. At ~200 the population is ~280K repos today (it has grown well past the original ~100K target); see [ADR 0003](../docs/decisions/0003-min-stars-threshold.md). |
| `--deadline-seconds`  | none    | Optional wall-clock cap; workers exit cleanly past this. |
| `--incremental`       | off     | Only crawl repos pushed since the last successful crawl. Falls back to a full crawl when no watermark exists yet; see [ADR 0015](../docs/decisions/0015-incremental-metadata-refresh.md). |
| `--since`             | none    | Override the watermark with an explicit ISO 8601 date/time. Implies incremental behaviour. |
| `--log-level`         | INFO    | DEBUG / INFO / WARNING / ERROR                    |

### Full vs incremental

The first run is a full crawl and records its *start* time in
`crawl_state` (migration 0005). Later runs with `--incremental`
(`make crawl-incremental`) read that watermark and append
`pushed:>=YYYY-MM-DD` to every star shard, so they only re-pull repos
with recent commits — minutes rather than ~25. The watermark advances
only on a clean, uninterrupted finish, so an aborted run is retried
from the same point.

Starring bumps no timestamp on GitHub, so incremental runs cannot see
star drift. Only a full crawl re-baselines star counts, which is what
the refresh workflow's monthly cron is for (ADR 0015).

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

A single token can fetch ~5000 READMEs/hour, so the top 20K takes ~4 hours.
The pass skips archived repos, so full coverage means the ~244K non-archived
rows — about 49 hours. Crashes are safe — restart picks up where it left off.

## CLI: enrichment and signal passes

Two later passes read the same corpus and write the retrieval side-tables
that ranking uses ([ADR 0020](../docs/decisions/0020-index-time-enrichment.md)).
Both are optional — with their tables empty, search behaves exactly as it
does without them.

### Awesome-list mining (`src.mine_awesome`) — `make mine-awesome`

Turns the ~2.7K awesome-list repos already in the corpus into
`repository_enrichment` rows (`source='awesome-mined'`): the category
vocabulary ("Frameworks", "Vector Database") that canonical repos' own
metadata lacks, taken from human-curated catalogs. No LLM involved. It
re-fetches each list's *full* README transiently (stored copies are
capped at 8KB, which beheads a catalog file) and stores only the mined
entries; `repositories.readme` is untouched.

```
python -m src.mine_awesome [--workers N] [--limit-lists N] [--dry-run]
                           [--prune-stale] [--sample N] [--log-level LEVEL]
```

| Flag             | Default | Description                                          |
| ---------------- | ------- | ---------------------------------------------------- |
| `--workers`      | 20      | Concurrent list fetches (REST budget: ~1 call/list).  |
| `--limit-lists`  | 10000   | Cap on lists to mine, by stars desc. The default covers all. |
| `--dry-run`      | off     | Fetch, parse, aggregate, report — write nothing.      |
| `--prune-stale`  | off     | Delete mined rows for repos no longer in any list. Off by default; destructive ops stay guarded. |
| `--sample`       | 10      | Log this many sample rows, most-listed first.         |

Each run is a full re-mine and upserts wholesale per
`(repo_id, 'awesome-mined')`, so it is idempotent.

### deps.dev signals (`src.deps_dev_pass`) — `make signals`

Fills `repository_signals` from Google's deps.dev API (free, keyless, no
token needed): the OpenSSF Scorecard for every repo deps.dev knows, plus
dependent counts for the head of the corpus. The ranking weight that
consumes dependent counts ships at 0.0 — the signal is stored and dark
until the eval promotes it.

```
python -m src.deps_dev_pass [--top-n N] [--scorecard-all] [--workers N]
                            [--max-age-days N] [--log-level LEVEL]
```

| Flag              | Default | Description                                         |
| ----------------- | ------- | --------------------------------------------------- |
| `--top-n`         | 3000    | Repos (by stars desc) in the per-repo dependents walk. |
| `--scorecard-all` | off     | Run the cheap batched scorecard pass over the whole corpus, not just `--top-n`. `make signals` sets this. |
| `--workers`       | 8       | Concurrency. Deliberately low — unauthenticated API. |
| `--max-age-days`  | 0       | Skip repos refreshed more recently than this (0 = refresh everything). |

Resumable and idempotent: rows upsert by `repo_id`.

> After mining (or an LLM enrichment collect), run
> `make enrichment-terms`. The search lane probes a pre-folded per-repo
> term table rather than the enrichment rows themselves
> ([`../sql/0011_repository_enrichment_terms.sql`](../sql/0011_repository_enrichment_terms.sql)
> explains why), and nothing rebuilds it automatically — until you do,
> new enrichment has no effect on retrieval. The deps.dev pass needs no
> such step.

## Schema

See [`../sql/0001_initial_schema.sql`](../sql/0001_initial_schema.sql) and
[`../sql/0002_readme_columns.sql`](../sql/0002_readme_columns.sql). The metadata
crawl populates everything except `readme`, `readme_status`, and
`readme_fetched_at`; the README pass fills those in. The two passes above
write to `repository_enrichment` (0009) and `repository_signals` (0010).

`readme_status` is informational: `ok` (fetched), `not_found` (repo has no
README), `empty` (file exists but blank), `error` (transient failure —
clear `readme_fetched_at` to retry).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the pure pieces where bugs are subtle and silent: the shard
generator (boundary correctness), the rate limiter (concurrency safety),
the README client's decode/empty/truncate handling, the awesome-list
parser and the miner's aggregation policy, and the deps.dev pass's
package/version selection. The HTTP layers are exercised end to end
against the real APIs, not mocked here.

## Design decisions

Significant architectural choices are documented in
[`../docs/decisions/`](../docs/decisions/) as Architecture Decision Records
(ADRs). Each ADR captures the context, the decision, alternatives that
were rejected, and what would change the decision later. They are
append-only — when a decision is revised, a new ADR supersedes the old
one rather than rewriting it.

## Known limitations

- **Single-token only.** Multi-token rotation would let us push past the
  5000 points/hour ceiling, but isn't needed for the ~267K-repo metadata
  crawl. The README pass is more rate-limit-bound — full README coverage of
  the whole corpus would benefit from rotation.
- **No resume for the metadata crawl.** A crash mid-crawl re-processes
  shards from scratch on the next run, and the incremental watermark is
  left where it was. The `ON CONFLICT DO UPDATE` makes this safe but
  wasteful. The README pass *does* support resume.
- **Renames aren't picked up.** The upsert refreshes the mutable fields
  (description, stars, topics, `pushed_at`, …) but leaves `full_name`,
  `name`, `owner`, and `url` at their first-crawled values, so a repo
  that moves keeps its old name until the row is deleted and re-crawled.
  Rare enough at this corpus size to be a known gap rather than a bug
  worth the unique-constraint handling a rename update would need.
- **Star counts drift between full crawls.** Starring bumps no
  timestamp, so `--incremental` cannot see it; only a full re-baseline
  refreshes counts. See [the backlog](../docs/backlog.md) for the
  reconcile sweep that would fix this properly.
