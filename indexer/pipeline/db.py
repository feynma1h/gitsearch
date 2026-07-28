"""Database operations for the indexer pipeline.

Reads repos that need embedding, writes embeddings back. Uses the same
``DATABASE_URL`` env var as the crawler.
"""

from __future__ import annotations

import logging
import os
from typing import List, Tuple

import asyncpg

from .document_builder import RepoForEmbedding

logger = logging.getLogger(__name__)


async def create_pool() -> asyncpg.Pool:
    """Create the shared connection pool.

    ``statement_cache_size=0`` because ``DATABASE_URL`` now points at a
    transaction-mode pooler — see ``crawler/src/db.py`` for the full
    reasoning.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set.")
    return await asyncpg.create_pool(
        dsn=dsn, min_size=2, max_size=10, statement_cache_size=0,
    )


# Pending = repos in `repositories` with no embedding row for this model yet.
# We only consider repos where the README pass has completed (status is set),
# because we want to embed the README when we have it.
_FETCH_PENDING_SQL = """
SELECT
    r.id,
    r.full_name,
    r.description,
    r.primary_language,
    r.topics,
    r.readme
FROM repositories r
LEFT JOIN repository_embeddings e
       ON e.repo_id = r.id AND e.model_name = $1
WHERE e.repo_id IS NULL
  AND r.is_archived = FALSE
  AND r.readme_status IS NOT NULL
ORDER BY r.stars DESC
LIMIT $2
"""


async def fetch_pending_repos(
    pool: asyncpg.Pool,
    model_name: str,
    limit: int,
) -> List[Tuple[str, RepoForEmbedding]]:
    """Return up to ``limit`` repos that still need embedding for ``model_name``.

    Returns a list of ``(repo_id, RepoForEmbedding)`` tuples. The repo_id
    is returned separately so it can be passed to :func:`upsert_embedding`
    without re-fetching.

    Ordered by stars desc so a partial run captures the most relevant
    repos first.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FETCH_PENDING_SQL, model_name, limit)

    return [
        (
            row["id"],
            RepoForEmbedding(
                full_name=row["full_name"],
                description=row["description"],
                primary_language=row["primary_language"],
                topics=list(row["topics"]) if row["topics"] else [],
                readme=row["readme"],
            ),
        )
        for row in rows
    ]


_UPSERT_EMBEDDING_SQL = """
INSERT INTO repository_embeddings (repo_id, model_name, embedding, source_hash)
VALUES ($1, $2, $3, $4)
ON CONFLICT (repo_id, model_name) DO UPDATE SET
    embedding   = EXCLUDED.embedding,
    source_hash = EXCLUDED.source_hash,
    embedded_at = NOW()
"""


async def upsert_embeddings(
    pool: asyncpg.Pool,
    rows: List[Tuple[str, str, List[float], str]],
) -> int:
    """Upsert a batch of (repo_id, model_name, embedding, source_hash) rows.

    pgvector accepts a Python list of floats directly via asyncpg with the
    appropriate codec registered, but to keep this dependency-light we send
    the vector as its string representation, which pgvector parses
    server-side. Slightly more bytes on the wire, much simpler client-side.
    """
    if not rows:
        return 0

    # Convert vectors to pgvector's string format: "[0.1,0.2,...]".
    serialised = [
        (repo_id, model, _vector_to_pg(emb), src_hash)
        for repo_id, model, emb, src_hash in rows
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(_UPSERT_EMBEDDING_SQL, serialised)

    return len(serialised)


def _vector_to_pg(vector: List[float]) -> str:
    """Format a vector for pgvector's text input syntax."""
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
