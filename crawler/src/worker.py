"""Worker coroutine: drains shards from the queue and persists results."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp
import asyncpg

from .db import insert_batch
from .github_client import GitHubAPIError, fetch_repos
from .rate_limiter import RateLimiter, parse_graphql_rate_limit

logger = logging.getLogger(__name__)


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    limiter: RateLimiter,
    session: aiohttp.ClientSession,
    token: str,
    pool: asyncpg.Pool,
    deadline: Optional[float] = None,
) -> None:
    """Pull shards from ``queue`` until empty (or ``deadline`` is reached).

    Args:
        worker_id: Index used in log lines for tracing.
        queue: Shared queue of GitHub search query fragments.
        limiter: Shared rate limiter.
        session: Shared aiohttp session for connection reuse.
        token: GitHub personal access token.
        pool: asyncpg connection pool.
        deadline: Optional ``time.monotonic()`` value past which the worker
            stops accepting new shards. Useful to bound total runtime.
    """
    while True:
        try:
            shard = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            await _process_shard(
                worker_id, shard, limiter, session, token, pool, deadline,
            )
        except GitHubAPIError as exc:
            logger.error("[w%d] Aborting shard %r: %s", worker_id, shard, exc)
        finally:
            queue.task_done()

        if deadline is not None and time.monotonic() > deadline:
            logger.warning("[w%d] Deadline reached; exiting.", worker_id)
            return


async def _process_shard(
    worker_id: int,
    shard: str,
    limiter: RateLimiter,
    session: aiohttp.ClientSession,
    token: str,
    pool: asyncpg.Pool,
    deadline: Optional[float],
) -> None:
    cursor: Optional[str] = None
    total = 0
    logger.info("[w%d] Starting shard %s", worker_id, shard)

    while True:
        if deadline is not None and time.monotonic() > deadline:
            logger.warning(
                "[w%d] Deadline hit mid-shard %s (fetched %d so far)",
                worker_id, shard, total,
            )
            return

        await limiter.wait_if_needed()
        data = await fetch_repos(session, token, shard, cursor)

        search = data["search"]
        repos = search["nodes"]
        await insert_batch(pool, repos)

        remaining, reset_at = parse_graphql_rate_limit(data["rateLimit"])
        await limiter.update(remaining, reset_at)

        total += len(repos)
        logger.debug(
            "[w%d] %s: +%d repos (total %d, rate=%d)",
            worker_id, shard, len(repos), total, limiter.remaining,
        )

        if not search["pageInfo"]["hasNextPage"]:
            break
        cursor = search["pageInfo"]["endCursor"]

    logger.info(
        "[w%d] Finished shard %s: %d repos (reported total: %d)",
        worker_id, shard, total, search["repositoryCount"],
    )
