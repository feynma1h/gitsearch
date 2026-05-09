"""Database access for the search service.

A single function: given a query vector + filters + weights, return the
top-N repos ranked by hybrid score.

The hybrid scoring math here intentionally mirrors ``ranking.py`` line by
line. The Python module is the source of truth for what the formula
*means*; this module is the source of truth for how it executes
efficiently in Postgres. They must stay in sync — any change to the
formula touches both files. The tests in ``tests/test_ranking.py`` pin
down the Python side; spot-checking against the SQL side is a manual
discipline.

Why compute the score in SQL rather than Python:
  - One round trip vs. two (fetch candidates, then re-fetch full rows).
  - Postgres can ORDER BY the computed score and LIMIT in one pass.
  - Top-N is small enough that re-ranking in Python would be cheap, but
    the DB is already there; no win to splitting.

See ADR 0013 for the full rationale.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

from .config import (
    DEFAULT_OVERFETCH_MAX,
    DEFAULT_OVERFETCH_MIN,
    DEFAULT_OVERFETCH_MULTIPLIER,
    HNSW_EF_SEARCH,
    MODEL_NAME,
)
from .ranking import LOG_STARS_DENOMINATOR, ScoringWeights

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchFilters:
    """Optional filters applied as SQL WHERE conditions.

    Each field is None to mean "don't filter on this." Empty lists also
    mean "don't filter" — an explicit empty topic list shouldn't filter
    everything out.
    """
    language: Optional[str] = None       # exact match on primary_language
    topics: Optional[List[str]] = None   # any-of (array overlap)
    min_stars: Optional[int] = None
    exclude_archived: bool = True        # default on; matches indexer's pending query


@dataclass(frozen=True)
class SearchHit:
    """One result row, in the order ``ORDER BY hybrid_score DESC``.

    The three ``*_contribution`` fields are the per-component additions
    to ``hybrid_score`` (already weight-multiplied), exposed so the UI
    can show *why* a result ranked where it did. Their sum equals
    ``hybrid_score``.
    """
    repo_id: str
    full_name: str
    description: Optional[str]
    url: str
    primary_language: Optional[str]
    topics: List[str]
    stars: int
    pushed_at: object  # datetime, but typing it that way pulls in datetime import here
    similarity: float
    hybrid_score: float
    similarity_contribution: float
    stars_contribution: float
    recency_contribution: float


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

async def create_pool() -> asyncpg.Pool:
    """Create the shared connection pool. Uses the same DATABASE_URL env
    var as the crawler and indexer."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set.")
    return await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)


# ---------------------------------------------------------------------------
# Search SQL
# ---------------------------------------------------------------------------

# The query is structured as a CTE:
#
#   1) `candidates` — pgvector HNSW lookup, top `$overfetch` by cosine
#      similarity, with WHERE filters applied. We over-fetch because the
#      hybrid re-rank may promote items that weren't in the top-N by
#      similarity alone (ADR 0013).
#
#   2) outer SELECT — joins back to `repositories` for display fields and
#      computes the hybrid score using the same formula as ranking.py:
#         similarity   = 1 - (embedding <=> query)
#         stars_norm   = LOG(1 + stars) / LOG_STARS_DENOMINATOR  (clamped <= 1)
#         recency_norm = 0.5 ^ (age_days / half_life)   [via EXP(-age*LN(2)/hl)]
#         hybrid       = w_sim * sim + w_stars * stars_norm + w_rec * recency_norm
#      Then ORDER BY hybrid DESC LIMIT $limit.
#
# Parameter slots:
#   $1  query vector (pgvector text format, "[v1,v2,...]")
#   $2  model_name
#   $3  overfetch (LIMIT inside the CTE)
#   $4  language filter or NULL
#   $5  topics filter (text[]) or NULL
#   $6  min_stars or NULL
#   $7  exclude_archived (bool)
#   $8  similarity weight
#   $9  stars weight
#   $10 recency weight
#   $11 half-life in days
#   $12 final LIMIT (the requested top-N)

_SEARCH_SQL = """
WITH candidates AS (
    SELECT
        e.repo_id,
        e.embedding,
        1 - (e.embedding <=> $1::vector) AS similarity
    FROM repository_embeddings e
    JOIN repositories r ON r.id = e.repo_id
    WHERE e.model_name = $2
      AND ($4::text       IS NULL OR r.primary_language = $4)
      AND ($5::text[]     IS NULL OR r.topics && $5::text[])
      AND ($6::int        IS NULL OR r.stars >= $6)
      AND ($7::bool = FALSE OR r.is_archived = FALSE)
    ORDER BY e.embedding <=> $1::vector
    LIMIT $3
),
scored AS (
    SELECT
        c.repo_id,
        c.similarity,
        $8  * GREATEST(0, LEAST(1, c.similarity))               AS similarity_contribution,
        $9  * LEAST(1.0, LOG(1 + r.stars) / {log_stars_denom})  AS stars_contribution,
        $10 * CASE
                WHEN r.pushed_at IS NULL THEN 0
                ELSE EXP(
                    - GREATEST(
                        0,
                        EXTRACT(EPOCH FROM (NOW() - r.pushed_at)) / 86400.0
                    ) * LN(2) / $11
                )
              END                                                AS recency_contribution
    FROM candidates c
    JOIN repositories r ON r.id = c.repo_id
)
SELECT
    r.id            AS repo_id,
    r.full_name,
    r.description,
    r.url,
    r.primary_language,
    r.topics,
    r.stars,
    r.pushed_at,
    s.similarity,
    s.similarity_contribution,
    s.stars_contribution,
    s.recency_contribution,
    (s.similarity_contribution + s.stars_contribution + s.recency_contribution) AS hybrid_score
FROM scored s
JOIN repositories r ON r.id = s.repo_id
ORDER BY hybrid_score DESC
LIMIT $12
""".format(log_stars_denom=LOG_STARS_DENOMINATOR)


def _vector_to_pg(vector: List[float]) -> str:
    """Format a vector for pgvector's text input syntax.

    Matches ``indexer/pipeline/db._vector_to_pg`` byte-for-byte. We
    duplicate it (rather than import) to keep this service installable
    without the indexer package on the path. The format is fixed by
    pgvector's wire protocol; both sides will agree mechanically.
    """
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def compute_overfetch(limit: int) -> int:
    """Pick how many candidates HNSW should return for re-ranking.

    Bounded above and below to keep latency predictable: we never
    over-fetch absurdly more than asked, but we also always have enough
    headroom for the hybrid re-rank to surface non-top-similarity hits.
    """
    target = max(DEFAULT_OVERFETCH_MIN, DEFAULT_OVERFETCH_MULTIPLIER * limit)
    return min(target, DEFAULT_OVERFETCH_MAX)


async def search(
    pool: asyncpg.Pool,
    *,
    query_vector: List[float],
    filters: SearchFilters,
    weights: ScoringWeights,
    limit: int,
) -> List[SearchHit]:
    """Run a hybrid vector + metadata search and return ranked hits."""

    overfetch = compute_overfetch(limit)
    query_vec_text = _vector_to_pg(query_vector)

    async with pool.acquire() as conn:
        # ef_search is a per-session GUC. Setting it inside a transaction
        # scopes it to this query only; concurrent connections aren't
        # affected.
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
            rows = await conn.fetch(
                _SEARCH_SQL,
                query_vec_text,
                MODEL_NAME,
                overfetch,
                filters.language,
                filters.topics if filters.topics else None,
                filters.min_stars,
                filters.exclude_archived,
                weights.similarity,
                weights.stars,
                weights.recency,
                weights.half_life_days,
                limit,
            )

    return [
        SearchHit(
            repo_id=row["repo_id"],
            full_name=row["full_name"],
            description=row["description"],
            url=row["url"],
            primary_language=row["primary_language"],
            topics=list(row["topics"]) if row["topics"] else [],
            stars=row["stars"],
            pushed_at=row["pushed_at"],
            similarity=float(row["similarity"]),
            hybrid_score=float(row["hybrid_score"]),
            similarity_contribution=float(row["similarity_contribution"]),
            stars_contribution=float(row["stars_contribution"]),
            recency_contribution=float(row["recency_contribution"]),
        )
        for row in rows
    ]
