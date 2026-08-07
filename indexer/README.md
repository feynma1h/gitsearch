# Indexer

Generates embeddings for crawled GitHub repositories and stores them in
pgvector for semantic search.

Two processes:

- **`service/`** — long-running embedding service (FastAPI + sentence-transformers)
- **`pipeline/`** — async pipeline that reads repos from Postgres, sends
  them to the service in batches, writes vectors back

See [`../docs/decisions/`](../docs/decisions/) for the rationale behind each
choice (ADRs 0005–0011).

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

# 4. After the pipeline finishes, build the HNSW index.
# (See ../sql/0003_repository_embeddings.sql for the exact statement.)
psql "$DATABASE_URL" -c "
CREATE INDEX CONCURRENTLY idx_repository_embeddings_hnsw
    ON repository_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"
```

Or use Docker Compose to run everything together — see
[`docker-compose.yml`](../docker-compose.yml) at the repo root.

## CLI: pipeline

```
python -m pipeline.main [--workers N] [--batch-size N] [--top-n N] [--log-level LEVEL]
```

| Flag           | Default | Description                                      |
| -------------- | ------- | ------------------------------------------------ |
| `--workers`    | 4       | Concurrent batches in flight.                    |
| `--batch-size` | 32      | Texts per HTTP call to the service.              |
| `--top-n`      | 20000   | Max repos per run, ordered by stars desc.        |
| `--log-level`  | INFO    | DEBUG / INFO / WARNING / ERROR                   |

The pipeline is **resumable**: only repos without an embedding row for
the current model are processed.

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

{first ~2KB of README}
```

See [`pipeline/document_builder.py`](pipeline/document_builder.py) for the
construction logic. This is the most consequential file in the indexer
for search quality — changes here directly affect what queries can find
which repos.

## Resuming and re-embedding

- **Crash mid-run** → just restart `pipeline.main`. It picks up where it
  left off because the resume query is `LEFT JOIN ... WHERE e.repo_id IS NULL`.
- **README updated for some repos** → clear those embeddings and rerun:
  ```sql
  DELETE FROM repository_embeddings
  WHERE repo_id IN (SELECT id FROM repositories WHERE crawled_at > '...');
  ```
- **New model** → just change `MODEL_NAME` in `pipeline/config.py` (and
  match it in the service env), then run. Old embeddings remain untouched.

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
