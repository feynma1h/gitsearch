# gitsearch

**Find GitHub repos by what they do, not just by their name.**

[Live demo →](https://feynma1h.github.io/gitsearch/)

```
"download videos from youtube"        →   ytdl-org/youtube-dl, yt-dlp/yt-dlp
"remove the background from an image" →   nadermx/backgroundremover, danielgatis/rembg
"kubernetes package manager"          →   helm/helm, zarf-dev/zarf
"fast http server in rust"            →   seanmonstar/warp, TheWaWaR/simple-http-server
```

Type a plain-English description of what you want, and get back GitHub
repositories ranked by how well they match — combining semantic relevance,
popularity, and recency. Click any result for a short, generated
step-by-step guide to actually using that repo.

The corpus is ~267,000 repositories crawled, ~244,000 fully indexed with
embeddings. A query whose vocabulary the database has cached returns in
~120 ms server-side; a genuinely novel one takes 1–3 s, because the
curation index is larger than the database instance's cache. Both
services also scale to zero when idle, so the first request after a long
quiet spell waits about a minute while they wake — a keep-warm ping every
10 minutes makes that rare.

---

## Try it

- **Fastest:** open the [live demo](https://feynma1h.github.io/gitsearch/) and
  type what you're looking for in plain English.
- **From the terminal**, against the deployed search API:

  ```bash
  SEARCH_URL=https://gitsearch-search-148185858207.asia-southeast1.run.app

  curl -s $SEARCH_URL/search \
    -H 'Content-Type: application/json' \
    -d '{"query": "fast http server in rust", "filters": {"language": "Rust"}}' | jq
  ```

You can filter by language, minimum stars, and topics, and tune how much each
ranking signal (relevance / popularity / recency) counts.

## What it is

A small but complete, production-shaped system in four parts, built around
one Postgres + pgvector database:

- **[`crawler/`](crawler/)** — fetches repository metadata from GitHub (GraphQL)
  and READMEs (REST) into Postgres. After a one-time full crawl, it refreshes
  incrementally — only repos created or updated since the last run.
- **[`indexer/`](indexer/)** — an embedding service (`bge-small-en-v1.5`) plus a
  pipeline that turns each repo into a vector and stores it in pgvector.
- **[`search/`](search/)** — the API that embeds your query, retrieves
  candidates through fused full-text + vector + name lanes, ranks them, and
  generates the per-repo "how do I use this?" guides.
- **[`frontend/`](frontend/)** — a single static HTML page, no build step.

The parts are deliberately independent: they share the database and talk over
HTTP, but share no Python code, so each can be built, tested, and deployed on
its own.

## How it works

```
              ┌───────────────────┐
              │   GitHub API      │
              │   GraphQL + REST  │
              └─────────┬─────────┘
                        │
                ┌───────▼────────┐
                │    crawler     │   full crawl once, then incremental
                │   (batch job)  │   refresh of new/changed repos
                └───────┬────────┘
                        │ writes
                        ▼
              ┌───────────────────┐
              │     Postgres      │
              │  repositories     │
              │  repository_      │
              │    embeddings     │
              └────┬────────▲─────┘
       reads ──────┘        │ writes
                            │
              ┌─────────────┴─────┐
              │  indexer pipeline │   embeds repos that need it
              │   (batch job)     │
              └──────┬────────────┘
                     │ uses
                     ▼
              ┌───────────────────┐         ┌───────────────────┐
              │ embedding service │ ◀────── │  search service   │
              │ POST /embed       │  embed  │ POST /search      │ ◀── user
              │                   │  query  │ GET  /guide/{id}  │
              └───────────────────┘         └─────────┬─────────┘
                                                      │ reads
                                                      ▼
                                              (pgvector + HNSW)
```

**Retrieval and ranking.** Candidates come from three lanes in one SQL
statement — full-text over name/topics/description/README, dense vectors
(pgvector HNSW), and fuzzy name matching for typos — fused by Reciprocal Rank
Fusion. The full-text lane also draws on *curation*: category labels and
descriptions mined from the awesome-lists that link a repo, which can supply
the one word its own metadata never says ("framework" for pytorch) but are
capped at completing a match, never building one — otherwise a megastar with
two stray curated words takes over every category query, which is exactly
what the first measurement showed. The final order blends
normalised signals: fused relevance, a *saturating* star count (popularity
boosts, but can never drown relevance), and recency with a floor (finished
classics don't sink). A fourth signal — how many published packages depend on
the repo — is wired through the same blend at weight zero, dark until the eval
says it earns a place. Typing a repo's exact name puts that repo first,
always. The search API returns each result's per-signal contribution, so the
UI can show *why* something ranked where it did.

**Usage guides.** Clicking a result asks the search service for a short,
standard step-by-step guide (what it is → prerequisites → install → run →
next step). A small language model reads the repo's actual files —
manifests, docs, examples — through a bounded tool loop, and the result is
cached, so repeat views are instant and free.

## Run it yourself

```bash
# 1. Configure
cp .env.example .env
# Fill in GITHUB_TOKEN and (for usage guides) ANTHROPIC_API_KEY.

# 2. Start Postgres, the embedding service, and the search service.
make up
make migrate-compose   # or 'make migrate' if you have psql on the host

# 3. Populate the corpus (batch jobs, run from the host).
python3 -m venv .venv && source .venv/bin/activate
make install
make crawl          # metadata:  ~25 min for ~267K repos
make readmes        # READMEs:    top 20K in ~4 hr (rate-limited)
make index          # embeddings: the 20K above, ~20 min
make build-hnsw-halfvec  # vector index; one-time, after indexing finishes

# 4. Search.
curl -s localhost:8002/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "fast http server in rust"}' | jq

# 5. Optional: measure search quality against a labelled query set.
make eval
```

That builds a demo-sized corpus. `make readmes` and `make index` each take
up to 20K repos per run and are resumable — to go further, re-run each until
it reports nothing pending. The full ~244K set took 13 indexing runs, about
four hours. Two further passes build the curation and dependency signals
the deployed service ranks with, and need no credentials beyond the GitHub
token you already have:

```bash
make mine-awesome      # category labels from the awesome-lists that link each repo
make enrichment-terms  # fold the mined text into the table the FTS lane probes
make signals           # dependent counts and scorecards from deps.dev
```

See [`make help`](Makefile) for all tasks, and
[ADR 0020](docs/decisions/0020-index-time-enrichment.md) for how curated
documents are embedded under their own label so the two generations can be
compared before either serves. Prefer not to use Docker? The
[full setup](#running-without-docker) works against any Postgres 14+ with
pgvector (the Supabase free tier is fine); the Compose stack pins 16.

## Where it runs

Serving runs on free tiers everywhere except the database:

| Component               | Runs on                        | Why                                                  |
| ----------------------- | ------------------------------ | ---------------------------------------------------- |
| Postgres + pgvector     | Supabase                       | Managed pgvector with backups                        |
| Embedding service       | Google Cloud Run               | Scales to zero between requests                      |
| Search service          | Google Cloud Run               | Same; a 10-min scheduler ping keeps both warm        |
| Frontend                | GitHub Pages                   | Static; deploys via GitHub Actions                   |
| Corpus refresh          | GitHub Actions                 | Weekly incremental pass; monthly full re-baseline — **schedules currently off**, see below |

The refresh is a chain of three workflows — metadata, then READMEs, then
embeddings — each chunked to fit inside a job's time limit and re-triggering
itself until its stage is drained
([ADR 0014](docs/decisions/0014-chunked-actions-refresh.md)).

The three workflows ran on their crons through July 2026 and are now
**disabled in the repository's Actions settings**: the corpus is a
snapshot as of its last run, not a continuously updated index. Nothing
about the code changed — the crons are still declared, and re-enabling
the workflows (or dispatching `Refresh Metadata` manually) restarts the
chain.

Two things to do first. Confirm the `DATABASE_URL`, `CRAWLER_GH_TOKEN`,
`EMBEDDING_SERVICE_URL`, and `WORKFLOW_DISPATCH_PAT` secrets are still
valid. Then run `python scripts/check_regression.py --rebaseline` once:
the stored health watermark holds an all-label embedding count from
before that check was scoped to the serving label, so without it the
first run's final health check reads the difference as a two-thirds
collapse and fails the workflow — after the whole chain has already run.
See [`docs/backlog.md`](docs/backlog.md).

Running cost is about **$30/month**, effectively all of it the managed
Postgres — Cloud Run, GitHub Pages, and Cloud Scheduler stay inside their
free tiers.

## Design decisions

Every non-trivial choice — the sharded crawling strategy, the incremental
refresh, the separate embeddings table, HNSW over IVFFlat, the hybrid scoring
formula, the usage-guide caching — is written up as a short **Architecture
Decision Record** in [`docs/decisions/`](docs/decisions/), including the
alternatives that were rejected and the conditions under which the decision
should be revisited. If you're planning to change something substantive, the
relevant ADR is the place to start.

Work that is designed but deliberately parked — each with the condition
that would activate it — lives in [`docs/backlog.md`](docs/backlog.md).

## Tests

```bash
make install-dev
make test
```

The unit tests focus on the parts where bugs are subtle and silent: the
crawler's shard-boundary math, rate limiter, and awesome-list parser, the
indexer's document construction and worker deadlines, and — on the search
side — the scoring math, the retrieval statement's input preparation, the
usage-guide tool loop, and the eval metrics themselves (cross-checked
against trec_eval). Anything that needs a live Postgres or a real HTTP
call is deliberately out of scope here; the [eval harness](search/eval/)
covers end-to-end search quality instead.

## Project layout

```
gitsearch/
├── README.md              ← this file
├── docker-compose.yml     ← postgres + embedding + search
├── Makefile               ← common tasks; see `make help`
├── .env.example
│
├── .github/workflows/     ← frontend deploy + corpus refresh
├── docs/decisions/        ← Architecture Decision Records
├── docs/backlog.md        ← designed but parked, with activation conditions
├── sql/                   ← migrations, applied in numeric order
├── scripts/               ← operational scripts (audit, progress, regression)
│
├── crawler/               ← GitHub metadata + README crawler
├── indexer/               ← embedding service + indexing pipeline
├── search/                ← search API, usage guides, and eval harness
└── frontend/              ← static UI
```

Each component has its own README with the details — flags, environment
variables, internal architecture, and known limitations.

## Known limitations

These are real, deliberate boundaries, not oversights:

- **Curated corpus, not all of GitHub.** The floor is 200 stars — about 280K
  repositories on GitHub today, ~267K of them crawled. Indexing every public
  repository (hundreds of millions) isn't feasible on free-tier
  infrastructure, and GitHub's search API can't even enumerate them — so the
  project tracks the active, popular slice rather than chasing completeness.
- **The deployed corpus is a snapshot.** The refresh pipeline is built,
  tested, and was running weekly; its schedules are currently switched off
  (see [Where it runs](#where-it-runs)), so the live demo searches a corpus
  frozen at its last run rather than a continuously updated one.
- **Single instance.** One crawler token, one embedding process, one search
  replica. Horizontal scaling is straightforward but unnecessary at this size.
- **Coverage-based lexical scoring, not BM25.** Postgres full-text search has
  no IDF; on this corpus's short, uniform documents that is second-order. A
  BM25 sidecar is the documented escape hatch if measurement ever says
  otherwise.

## Running without Docker

```bash
# Bring your own Postgres 14+ with pgvector (Supabase free tier works).
export DATABASE_URL=postgresql://...
export GITHUB_TOKEN=ghp_...
export ANTHROPIC_API_KEY=sk-ant-...   # optional, for usage guides

python3 -m venv .venv && source .venv/bin/activate
make install
make migrate
make serve-embed &        # background
make crawl
make readmes              # re-run until nothing pending
make index                # re-run until nothing pending
make build-hnsw-halfvec
make serve-search &
make eval                 # sanity check
```

## License

MIT — see [LICENSE](LICENSE).
