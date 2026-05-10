"""Worker coroutine: drains shards from the queue and persists results.

Per-shard outcomes are recorded into a shared ``CrawlStats`` object so
that ``main`` can print a single-line summary at the end of the run —
the difference between "looks fine" and "76 of 85 shards aborted" is
otherwise invisible without grepping logs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp
import asyncpg

from .db import insert_batch
from .github_client import GitHubAPIError, fetch_repos
from .rate_limiter import RateLimiter, parse_graphql_rate_limit

logger = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    """Per-run summary, populated by workers as they finish shards.

    Owned by main(); workers append outcomes via the helper methods.
    A simple dataclass rather than a thread-safe counter because all
    mutations happen from the asyncio loop — no concurrent writes."""
    shards_total: int = 0
    shards_completed: int = 0
    shards_aborted: int = 0
    repos_inserted: int = 0
    aborted_shard_names: List[str] = field(default_factory=list)

    def record_completed(self, repos: int) -> None:
        self.shards_completed += 1
        self.repos_inserted += repos

    def record_aborted(self, shard: str) -> None:
        self.shards_aborted += 1
        self.aborted_shard_names.append(shard)


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    limiter: RateLimiter,
    session: aiohttp.ClientSession,
    token: str,
    pool: asyncpg.Pool,
    stats: CrawlStats,
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
        stats: Shared accumulator for the end-of-run summary.
        deadline: Optional ``time.monotonic()`` value past which the worker
            stops accepting new shards. Useful to bound total runtime.
    """

    # Per-worker startup stagger. All workers are created in a tight
    # loop in main.py, so without this they all hit GitHub within
    # milliseconds — exactly the burst pattern that trips the secondary
    # rate limit on first contact, especially from Actions runners
    # whose egress IPs are flagged more aggressively. 1.5s spacing
    # keeps the burst signal below GitHub's detection threshold.
    await asyncio.sleep(worker_id * 20.0)

    while True:
        try:
            shard = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            repos = await _process_shard(
                worker_id, shard, limiter, session, token, pool, deadline,
            )
            stats.record_completed(repos)
        except GitHubAPIError as exc:
            logger.error("[w%d] Aborting shard %r: %s", worker_id, shard, exc)
            stats.record_aborted(shard)
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
) -> int:
    """Fetch all pages for one shard. Returns the number of repos inserted."""
    cursor: Optional[str] = None
    total = 0
    logger.info("[w%d] Starting shard %s", worker_id, shard)

    while True:
        if deadline is not None and time.monotonic() > deadline:
            logger.warning(
                "[w%d] Deadline hit mid-shard %s (fetched %d so far)",
                worker_id, shard, total,
            )
            return total

        await limiter.wait_if_needed()
        # Pass the limiter so secondary-rate-limit detection in the
        # GitHub client can trigger a global pause across all workers.
        # Without this, a 403 throttles only the worker that hit it
        # while siblings keep firing requests, prolonging the throttle.
        data = await fetch_repos(session, token, shard, cursor, limiter=limiter)

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
    return total
