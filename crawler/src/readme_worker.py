"""Worker coroutine for the README fetching pass.

Each worker pulls ``(id, owner, name)`` tuples from a shared queue, fetches
the README via the REST API, and writes the result back to Postgres. The
shared :class:`RateLimiter` paces requests across all workers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import asyncpg

from .db import update_readme
from .rate_limiter import RateLimiter
from .readme_client import ReadmeClient

logger = logging.getLogger(__name__)


async def readme_worker(
    worker_id: int,
    queue: asyncio.Queue,
    limiter: RateLimiter,
    client: ReadmeClient,
    pool: asyncpg.Pool,
    deadline: Optional[float] = None,
    progress: Optional["ProgressCounter"] = None,
) -> None:
    """Drain the queue, fetching READMEs until empty or deadline reached."""
    while True:
        try:
            repo_id, owner, name = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            await _fetch_one(
                worker_id, repo_id, owner, name, limiter, client, pool,
            )
            if progress is not None:
                progress.tick()
        except Exception as exc:  # noqa: BLE001 — log + continue on per-repo failure
            logger.exception(
                "[r%d] Unexpected error fetching %s/%s: %s",
                worker_id, owner, name, exc,
            )
        finally:
            queue.task_done()

        if deadline is not None and time.monotonic() > deadline:
            logger.warning("[r%d] Deadline reached; exiting.", worker_id)
            return


async def _fetch_one(
    worker_id: int,
    repo_id: str,
    owner: str,
    name: str,
    limiter: RateLimiter,
    client: ReadmeClient,
    pool: asyncpg.Pool,
) -> None:
    # The client itself updates the limiter from response headers, so we
    # only need to wait *before* the call.
    await limiter.wait_if_needed()
    result = await client.fetch(owner, name)

    await update_readme(pool, repo_id, result.content, result.status)

    if result.status == "ok":
        logger.debug("[r%d] %s/%s: ok (%d chars)",
                     worker_id, owner, name, len(result.content or ""))
    elif result.status == "not_found":
        logger.debug("[r%d] %s/%s: no README", worker_id, owner, name)
    elif result.status == "empty":
        logger.debug("[r%d] %s/%s: empty README", worker_id, owner, name)
    else:
        logger.warning(
            "[r%d] %s/%s: %s (%s)",
            worker_id, owner, name, result.status, result.error_detail,
        )


class ProgressCounter:
    """Lightweight progress counter shared across workers.

    Logs a heartbeat every ``log_every`` ticks so a long run shows life
    without spamming the log per request.
    """

    def __init__(self, total: int, log_every: int = 100) -> None:
        self._total = total
        self._done = 0
        self._log_every = log_every
        self._start = time.monotonic()

    def tick(self) -> None:
        # Lock-free increment is safe because asyncio is single-threaded;
        # there are no preemption points inside this function.
        self._done += 1
        if self._done % self._log_every == 0 or self._done == self._total:
            elapsed = time.monotonic() - self._start
            rate = self._done / elapsed if elapsed > 0 else 0
            remaining = self._total - self._done
            eta = remaining / rate if rate > 0 else float("inf")
            logger.info(
                "Progress: %d/%d (%.1f%%) | %.1f req/s | ETA %.0fs",
                self._done, self._total,
                100 * self._done / self._total,
                rate, eta,
            )
