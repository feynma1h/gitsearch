"""Postgres connection pool and batch insert for repository rows."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)


async def create_pool() -> asyncpg.Pool:
    """Create the shared connection pool. Reads ``DATABASE_URL`` from env."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Example: postgresql://user:pass@localhost:5432/crawler"
        )
    return await asyncpg.create_pool(dsn=dsn, min_size=5, max_size=20)


_INSERT_SQL = """
INSERT INTO repositories (
    id, full_name, name, owner, description, url, homepage_url,
    primary_language, topics, stars, forks, is_archived, is_fork,
    created_at, pushed_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
)
ON CONFLICT (id) DO UPDATE SET
    description      = EXCLUDED.description,
    homepage_url     = EXCLUDED.homepage_url,
    primary_language = EXCLUDED.primary_language,
    topics           = EXCLUDED.topics,
    stars            = EXCLUDED.stars,
    forks            = EXCLUDED.forks,
    is_archived      = EXCLUDED.is_archived,
    pushed_at        = EXCLUDED.pushed_at,
    crawled_at       = NOW();
"""


def _parse_iso(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _to_row(repo: Dict[str, Any]) -> Tuple[Any, ...]:
    """Flatten a GraphQL Repository node into a tuple matching ``_INSERT_SQL``."""
    full_name = repo["nameWithOwner"]
    owner = repo["owner"]["login"]
    primary_language = (
        repo["primaryLanguage"]["name"] if repo.get("primaryLanguage") else None
    )
    topics = [
        node["topic"]["name"]
        for node in repo.get("repositoryTopics", {}).get("nodes", [])
    ]
    return (
        repo["id"],
        full_name,
        repo["name"],
        owner,
        repo.get("description"),
        repo["url"],
        repo.get("homepageUrl"),
        primary_language,
        topics,
        repo["stargazerCount"],
        repo["forkCount"],
        repo["isArchived"],
        repo["isFork"],
        _parse_iso(repo["createdAt"]),
        _parse_iso(repo.get("pushedAt")),
    )


async def insert_batch(pool: asyncpg.Pool, repos: Iterable[Dict[str, Any]]) -> int:
    """Upsert a batch of repos. Returns the number of rows processed."""
    rows: List[Tuple[Any, ...]] = [_to_row(r) for r in repos]
    if not rows:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(_INSERT_SQL, rows)

    return len(rows)


# ---------------------------------------------------------------------------
# README pass helpers
# ---------------------------------------------------------------------------

_FETCH_PENDING_SQL = """
SELECT id, owner, name
FROM repositories
WHERE readme_fetched_at IS NULL
  AND is_archived = FALSE
ORDER BY stars DESC
LIMIT $1
"""


async def fetch_pending_readmes(
    pool: asyncpg.Pool,
    limit: int,
) -> List[Tuple[str, str, str]]:
    """Return up to ``limit`` repos that still need their README fetched.

    Ordered by stars descending so a partial run captures the most relevant
    repos first. Archived repos are skipped — their READMEs are unlikely to
    change and most of them have empty or templated content anyway.

    Returns a list of ``(id, owner, name)`` tuples.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FETCH_PENDING_SQL, limit)
    return [(r["id"], r["owner"], r["name"]) for r in rows]


_UPDATE_README_SQL = """
UPDATE repositories
SET readme            = $2,
    readme_status     = $3,
    readme_fetched_at = NOW()
WHERE id = $1
"""


async def update_readme(
    pool: asyncpg.Pool,
    repo_id: str,
    content: Optional[str],
    status: str,
) -> None:
    """Persist the result of one README fetch.

    Args:
        repo_id: GraphQL node ID (matches ``repositories.id``).
        content: README text, or None for non-'ok' statuses.
        status: One of 'ok', 'not_found', 'empty', 'error'.
    """
    async with pool.acquire() as conn:
        await conn.execute(_UPDATE_README_SQL, repo_id, content, status)


# ---------------------------------------------------------------------------
# Incremental-crawl watermark (see ADR 0015)
# ---------------------------------------------------------------------------

_GET_LAST_CRAWL_SQL = "SELECT last_metadata_crawl_at FROM crawl_state WHERE id = 1"

_SET_LAST_CRAWL_SQL = """
INSERT INTO crawl_state (id, last_metadata_crawl_at, updated_at)
VALUES (1, $1, NOW())
ON CONFLICT (id) DO UPDATE SET
    last_metadata_crawl_at = EXCLUDED.last_metadata_crawl_at,
    updated_at             = NOW()
"""


async def get_last_crawl_at(pool: asyncpg.Pool) -> Optional[datetime]:
    """Return the start time of the last successful metadata crawl, or None.

    None means no full crawl has completed yet — the caller should fall back
    to a full crawl rather than an incremental one.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_LAST_CRAWL_SQL)
    return row["last_metadata_crawl_at"] if row else None


async def set_last_crawl_at(pool: asyncpg.Pool, ts: datetime) -> None:
    """Record ``ts`` as the watermark for the next incremental crawl.

    Pass the *start* time of the run that just completed, not its end time,
    so the next run re-includes anything pushed while this one was running.
    Re-includes are harmless — ``insert_batch`` upserts by id.
    """
    async with pool.acquire() as conn:
        await conn.execute(_SET_LAST_CRAWL_SQL, ts)
