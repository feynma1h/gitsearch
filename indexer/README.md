# Indexer

Generates embeddings for crawled GitHub repositories and stores them in
pgvector for semantic search.

Two processes:

- **`service/`** — long-running embedding service (FastAPI + sentence-transformers)
- **`pipeline/`** — async pipeline that reads repos from Postgres, sends
  them to the service in batches, writes vectors back

Plus one optional batch job, [`pipeline/enrich_llm.py`](#llm-enrichment-optional),
which generates the search-index enrichment text that a "+enrich" label
embeds.

See [`../docs/decisions/`](../docs/decisions/) for the rationale behind each
choice (ADRs 0005–0011, plus [0020](../docs/decisions/0020-index-time-enrichment.md)
for enrichment and versioned embedding labels).

## Setup

This assumes the crawler has already populated the `repositories` table.
The indexer reads from that table and writes to a new
`repository_embeddings` table.

```bash
# 1. Apply the migration to add pgvector + the embeddings table.
psql "$DATABASE_URL" -f ../sql/0003_repository_embeddings.sql

# 2. Start the embedding service (in its own terminal/container).
# Run from indexer/, so that "service" resolves as a package.
pip install -r service/requirements.txt
uvicorn service.server:app --host 0.0.0.0 --port 8001
# Wait until you see "Service ready with model BAAI/bge-small-en-v1.5".

# 3. In a separate terminal, run the pipeline (also from indexer/).
pip install -r pipeline/requirements.txt
DATABASE_URL=postgresql://... \
EMBEDDING_SERVICE_URL=http://localhost:8001 \
    python -m pipeline.main --top-n 20000

# 4. After the pipeline finishes, build the vector index. It's a `make`
# target at the repo root rather than a psql one-liner because the build
# needs session-scoped tuning a pooled connection won't carry.
make build-hnsw-halfvec
```

The index is half-precision and **partial per model label** — that
predicate is load-bearing, not an optimisation. See "Model labels" below.

Or use Docker Compose to run everything together — see
[`docker-compose.yml`](../docker-compose.yml) at the repo root.

## CLI: pipeline

```
python -m pipeline.main [--workers N] [--batch-size N] [--top-n N]
                        [--deadline-seconds N] [--log-level LEVEL]
```

| Flag                 | Default | Description                                      |
| -------------------- | ------- | ------------------------------------------------ |
| `--workers`          | 4       | Concurrent batches in flight.                    |
| `--batch-size`       | 32      | Texts per HTTP call to the service.              |
| `--top-n`            | 20000   | Max repos per run, ordered by stars desc.        |
| `--deadline-seconds` | none    | Stop pulling new batches after this many seconds; in-flight batches finish. Used by the chunked refresh workflow ([ADR 0014](../docs/decisions/0014-chunked-actions-refresh.md)). |
| `--log-level`        | INFO    | DEBUG / INFO / WARNING / ERROR                   |

The pipeline is **resumable**: only repos without an embedding row for
the current label are processed.

## Service endpoints

```
GET  /health
POST /embed   { "texts": ["...", "..."], "model"?: "..." }
              → { "embeddings": [[...], [...]], "model": "..." }
```

The service is stateless aside from the loaded model. Restart-safe.

## What gets embedded

The pipeline builds a "search document" per repo with the most signal-rich
fields first, so even aggressive truncation preserves the strongest signals:

```
{full_name}: {description}
Language: {primary_language}
Topics: {topics joined}
Also known as: {aliases joined}         ─┐
Categories: {categories joined}          │  enrichment — empty unless the
Common queries: {queries joined}         │  run uses a "+enrich" label
{enrichment description}                ─┘

{first ~2KB of README}
```

See [`pipeline/document_builder.py`](pipeline/document_builder.py) for the
construction logic. This is the most consequential file in the indexer
for search quality — changes here directly affect what queries can find
which repos.

## Model labels

`repository_embeddings` is keyed `(repo_id, model_name)`, so several
generations of vectors sit side by side (ADR 0006). The label names the
encoder *plus* the document construction:

| Label                              | Document                              |
| ---------------------------------- | ------------------------------------- |
| `BAAI/bge-small-en-v1.5`           | metadata + README (the layout above)  |
| `BAAI/bge-small-en-v1.5+enrich-v1` | the same, with enrichment folded in   |

Set `EMBEDDINGS_MODEL_LABEL` to pick which label a pipeline run writes.
A label containing `+enrich` makes the run scope itself to enriched
repos and fold their `repository_enrichment` text into each document.
The *encoder* never changes with the label — the embedding service
loads its own model from `EMBEDDING_MODEL`, and the pipeline requests
that encoder by name (the label minus its `+suffix`), so query and
document vectors always share a space.

The search service picks which label it serves with the same env var.
The label workflow — build the partial index, copy unchanged vectors
rather than recomputing them, embed only what changed — is scripted as
`make copy-embeddings-label` / `make index-enriched` /
`make build-hnsw-halfvec`; the Makefile records the order and why it
matters.

## LLM enrichment (optional)

[`pipeline/enrich_llm.py`](pipeline/enrich_llm.py) generates the
`source='llm'` half of `repository_enrichment` — 5–8 synthetic queries,
a short plain-vocabulary description, aliases, and category tags per
repo (Doc2Query--, ADR 0020). It is a *batch* job against a provider's
batch API, it costs real money at corpus scale, and nothing in the
serving path depends on it: with the rows absent, retrieval is exactly
what mined enrichment alone produces.

```bash
# 1. Estimate scope + cost. No API calls, no writes.
python -m pipeline.enrich_llm --top-n 20000

# 2. Submit. Prints the estimate again and refuses to spend without
#    the explicit flag.
GEMINI_API_KEY=... python -m pipeline.enrich_llm --top-n 20000 \
    --submit --i-approve-the-cost

# 3. Collect, filter, and write rows. Needs the embedding service up:
#    generated queries are kept only if they embed close enough to
#    their source document (the "--" in Doc2Query--).
python -m pipeline.enrich_llm --collect
```

`--provider` picks the generator (`gemini` by default, `anthropic`
available); every row records the model and prompt version that
produced it, so a prompt revision can regenerate selectively. Submitted
batch ids land in a gitignored `pipeline/.llm_batches.json` so
`--collect` can run later.

After collecting, run `make enrichment-terms` — the search lane probes a
pre-folded term table, not the enrichment rows, and nothing rebuilds it
automatically.

## Resuming and re-embedding

- **Crash mid-run** → just restart `pipeline.main`. It picks up where it
  left off because the resume query is `LEFT JOIN ... WHERE e.repo_id IS NULL`.
- **README updated for some repos** → clear those embeddings and rerun:
  ```sql
  DELETE FROM repository_embeddings
  WHERE repo_id IN (SELECT id FROM repositories WHERE crawled_at > '...');
  ```
- **New model or new document construction** → run under a different
  `EMBEDDINGS_MODEL_LABEL` (and, for a genuinely different encoder,
  point the service's `EMBEDDING_MODEL` at it too). Old embeddings
  remain untouched; serving flips with the same env var.

## Known limitations

- **Vector dimension is hardcoded** to 384 in the SQL schema. Swapping to
  a model with a different dimension means a schema change. Most
  practical alternatives (bge-base, nomic) use 768 dimensions, which
  would require a separate table or a migration.
- **No re-embedding on `source_hash` mismatch** yet. The hash is stored
  but the resume logic only checks "no row exists." A future cleanup pass
  could find rows where the source has changed and re-embed selectively.
- **Single embedding service instance.** Horizontal scaling would need a
  load balancer and stateless replica deployment, both straightforward
  but not yet wired up.
