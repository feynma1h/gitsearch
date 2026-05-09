"""Crawler entry point.

Usage:
    GITHUB_TOKEN=ghp_xxx DATABASE_URL=postgresql://... python -m src.main

CLI flags let you tune concurrency, the star floor, and an overall deadline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import Optional

import aiohttp

from .config import DEFAULT_METADATA_WORKERS, DEFAULT_MIN_STARS
from .db import create_pool
from .rate_limiter import RateLimiter
from .shard_generator import generate_shards
from .worker import CrawlStats, worker

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl GitHub repositories.")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_METADATA_WORKERS,
        help=f"Number of concurrent worker coroutines "
             f"(default: {DEFAULT_METADATA_WORKERS}).",
    )
    parser.add_argument(
        "--min-stars", type=int, default=DEFAULT_MIN_STARS,
        help=f"Lower bound on stars (default: {DEFAULT_MIN_STARS}). "
             f"~200 yields the target ~100K repos; see ADR 0003.",
    )
    parser.add_argument(
        "--deadline-seconds", type=int, default=None,
        help="Optional wall-clock limit; workers exit cleanly past this.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run(args: argparse.Namespace) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required.")

    shards = generate_shards(min_stars=args.min_stars)
    queue: asyncio.Queue = asyncio.Queue()
    for shard in shards:
        queue.put_nowait(shard)

    logger.info("Generated %d shards (min_stars=%d)", len(shards), args.min_stars)

    stats = CrawlStats(shards_total=len(shards))
    limiter = RateLimiter(name="graphql")
    pool = await create_pool()

    deadline: Optional[float] = None
    if args.deadline_seconds is not None:
        deadline = time.monotonic() + args.deadline_seconds

    # Allow Ctrl-C to cancel cleanly. On SIGINT, we cancel the gather() task.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; default behaviour ok.
            pass

    connector = aiohttp.TCPConnector(limit=100)
    start = time.monotonic()

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            workers = [
                asyncio.create_task(
                    worker(i, queue, limiter, session, token, pool, stats, deadline)
                )
                for i in range(args.workers)
            ]
            stop_task = asyncio.create_task(stop_event.wait())

            done, pending = await asyncio.wait(
                [*workers, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_task in done:
                logger.warning("Shutdown signal received; cancelling workers.")
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            else:
                stop_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
    finally:
        await pool.close()

    elapsed = time.monotonic() - start
    logger.info("Crawl finished in %.1fs", elapsed)

    # Always print the summary, even on early exit. Without this the only
    # signal that the crawl went badly is a flood of ERROR lines mid-log,
    # which is easy to miss. With it, "9 of 85 shards completed" is right
    # there at the end.
    logger.info(
        "Summary: %d/%d shards completed, %d aborted, %d repos inserted.",
        stats.shards_completed, stats.shards_total,
        stats.shards_aborted, stats.repos_inserted,
    )
    if stats.shards_aborted:
        logger.warning(
            "Aborted shards (likely candidates for re-running with lower "
            "concurrency): %s",
            ", ".join(stats.aborted_shard_names[:10])
            + ("..." if len(stats.aborted_shard_names) > 10 else ""),
        )


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
