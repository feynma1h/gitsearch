# Evaluation harness

A small offline harness for measuring search quality. Run on demand
when tuning weights, swapping the embedding model, or changing the
indexer's source-document construction.

The harness has one job: hit `POST /search` for each query in
`queries.json` and report Recall@K and NDCG@K, both per-query and
aggregate. It is **not** a unit test — it talks to a live service
over HTTP and prints results for you to read.

## Usage

```bash
# 1. Make sure search + embedding services are up and Postgres has
#    real data.

# 2. Baseline run (uses defaults from queries.json and service config).
python -m eval.run

# 3. Compare configurations by overriding weights:
python -m eval.run --weights similarity=1,stars=0,recency=0  # pure semantic
python -m eval.run --weights similarity=1,stars=0.5,recency=0.3
python -m eval.run --weights similarity=0.7,stars=0.5,recency=0.2

# 4. Save runs as JSON to diff between configurations:
python -m eval.run --json > runs/baseline.json
python -m eval.run --weights stars=0,recency=0 --json > runs/no-popularity.json
diff <(jq .aggregate runs/baseline.json) <(jq .aggregate runs/no-popularity.json)
```

## Metrics

- **Recall@K** — of the labelled relevant items, how many appeared in
  top-K. Easy to interpret: `0.6` means we found 60% of the right
  repos in the top-K results.
- **NDCG@K** — ranking-aware. Gives more credit for relevant items
  appearing higher in the list. `1.0` is a perfect ranking;
  `~0.7` is a reasonable target for a portfolio-quality system.

Both metrics treat relevance as binary. A query whose `relevant` list
is empty contributes `1.0` (vacuously satisfied) so it doesn't drag
the aggregate down — but you should remove such queries from the set;
they're not measuring anything.

The default sort in human-readable output is **worst NDCG first**.
That's deliberate — when tuning, you want to see the queries you're
failing on, not the easy wins.

## Growing the query set

`queries.json` ships with 5 seeds. Useful query types to add:

- **Specific repo names** that should be #1 (e.g., "redis" → `redis/redis`).
  These check that the embedder + ranker can handle exact-name lookups,
  which is a known weak spot of pure dense retrieval.
- **Conceptual queries** with multiple plausible answers (e.g.,
  "static site generator", "vector database").
- **Multi-word phrases** that test compositional understanding ("fast
  http server in rust" should bias toward Rust HTTP libraries, not
  generic Rust repos or generic HTTP repos).
- **Common failure modes** you've fixed before. If you noticed the
  ranker doing something dumb and tweaked weights to fix it, add a
  query that catches the regression.

A reasonable target is ~30 queries. The harness runs in a couple of
seconds per query (one embed call + one DB query), so the whole set
is under a minute.

## What the harness deliberately does *not* do

- **Doesn't tune weights for you.** Grid search is easy to add (loop
  over weight combinations, find the best NDCG) but optimising for
  the labelled set is overfitting unless the set is large and varied.
  Eyeball the worst queries first.
- **Doesn't grade relevance.** Binary is enough at this scale. If you
  later need fine-grained ("perfect / good / okay / bad"), extend
  `queries.json` with a `relevance` integer per item and update
  `ndcg_at_k` to use graded gains.
- **Doesn't measure latency.** `took_ms` is in the search response,
  but tracking it across runs is a different concern. Add it if a
  weight change visibly slows things down.
