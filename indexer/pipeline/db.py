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

    async def _init(conn: asyncpg.Connection) -> None:
        # Supabase sets statement_timeout on the role, so long pending
        # fetches get cancelled even through the session pooler. Lift it
        # per connection; only sticks in session mode (port 5432), which
        # is where the long fetches run.
        await conn.execute("SET statement_timeout = 0")

    return await asyncpg.create_pool(
        dsn=dsn, min_size=2, max_size=10, statement_cache_size=0,
        init=_init,
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

# The "+enrich" variant scopes pending to repos that HAVE enrichment
# rows. Repos without enrichment build byte-identical documents under
# either label, so their vectors are copied between labels SQL-side
# (make copy-embeddings-label) instead of being re-embedded; scoping
# here keeps an enrich-label run proportional to the enriched set and
# keeps label coverage a controlled experiment (old coverage + nothing
# else — a never-embedded repo doesn't sneak in just because a run
# used a bigger --top-n). Driven FROM the enrichment table (~50K
# distinct repos) rather than filtering all of `repositories`: the
# repositories-driven EXISTS shape ran past the transaction pooler's
# statement timeout.
_FETCH_PENDING_ENRICHED_SQL = """
SELECT
    r.id,
    r.full_name,
    r.description,
    r.primary_language,
    r.topics,
    r.readme
FROM (SELECT DISTINCT repo_id FROM repository_enrichment) en
JOIN repositories r ON r.id = en.repo_id
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
    include_enrichment: bool = False,
) -> List[Tuple[str, RepoForEmbedding]]:
    """Return up to ``limit`` repos that still need embedding for ``model_name``.

    Returns a list of ``(repo_id, RepoForEmbedding)`` tuples. The repo_id
    is returned separately so it can be passed to :func:`upsert_embedding`
    without re-fetching.

    Ordered by stars desc so a partial run captures the most relevant
    repos first.

    With ``include_enrichment`` (the "+enrich" labels, ADR 0020) the
    pending set narrows to enriched repos and each repo carries its
    enrichment fields for the document builder.
    """
    sql = _FETCH_PENDING_ENRICHED_SQL if include_enrichment else _FETCH_PENDING_SQL
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, model_name, limit)

    enrichment: dict = {}
    if include_enrichment and rows:
        enrichment = await _fetch_enrichment(
            pool, [row["id"] for row in rows]
        )

    out: List[Tuple[str, RepoForEmbedding]] = []
    for row in rows:
        extra = enrichment.get(row["id"], {})
        out.append((
            row["id"],
            RepoForEmbedding(
                full_name=row["full_name"],
                description=row["description"],
                primary_language=row["primary_language"],
                topics=list(row["topics"]) if row["topics"] else [],
                readme=row["readme"],
                aliases=extra.get("aliases", []),
                categories=extra.get("categories", []),
                queries=extra.get("queries", []),
                enrichment_description=extra.get("description"),
            ),
        ))
    return out


_FETCH_ENRICHMENT_ROWS_SQL = """
SELECT repo_id, source, description, queries, aliases, categories
FROM repository_enrichment
WHERE repo_id = ANY($1::text[])
ORDER BY repo_id, source
"""


async def _fetch_enrichment(
    pool: asyncpg.Pool, repo_ids: List[str], chunk: int = 10_000
) -> dict:
    """Fetch and fold enrichment rows for ``repo_ids``.

    Folding (array concat in source order, newline-joined descriptions)
    happens here in Python — at most two short rows per repo, and the
    dict shape is what RepoForEmbedding wants.
    """
    def _merge(slot: dict, row) -> None:
        for key in ("aliases", "categories", "queries"):
            for value in (row[key] or []):
                if value not in slot[key]:
                    slot[key].append(value)
        if row["description"]:
            slot["_descs"].append(row["description"])

    out: dict = {}
    for i in range(0, len(repo_ids), chunk):
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _FETCH_ENRICHMENT_ROWS_SQL, repo_ids[i:i + chunk]
            )
        for row in rows:
            slot = out.setdefault(row["repo_id"], {
                "aliases": [], "categories": [], "queries": [], "_descs": [],
            })
            _merge(slot, row)
    for slot in out.values():
        slot["description"] = "\n".join(slot.pop("_descs")) or None
    return out


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
