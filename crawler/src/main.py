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
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import DEFAULT_METADATA_WORKERS, DEFAULT_MIN_STARS
from .db import create_pool, get_last_crawl_at, set_last_crawl_at
from .rate_limiter import RateLimiter
from .shard_generator import apply_pushed_since, generate_shards
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
             f"~200 currently yields ~280K repos; see ADR 0003.",
    )
    parser.add_argument(
        "--deadline-seconds", type=int, default=None,
        help="Optional wall-clock limit; workers exit cleanly past this.",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Only crawl repos pushed since the last successful crawl "
             "(watermark read from crawl_state). Falls back to a full crawl "
             "if no watermark exists yet. See ADR 0015.",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Override the incremental watermark with an explicit ISO 8601 "
             "date/time (e.g. 2026-07-01). Implies incremental behaviour.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def _parse_since(value: str) -> datetime:
    """Parse an ISO 8601 date/datetime into a timezone-aware datetime (UTC)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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

    # Capture the run start time up front. On successful completion this
    # becomes the new watermark, so the next incremental run picks up
    # anything pushed while this run was in flight (ADR 0015).
    run_started_at = datetime.now(timezone.utc)
    pool = await create_pool()

    # Resolve the incremental watermark, if any.
    since: Optional[datetime] = None
    if args.since:
        since = _parse_since(args.since)
    elif args.incremental:
        try:
            since = await get_last_crawl_at(pool)
        except Exception as exc:
            logger.warning("Could not read crawl watermark (%s); full crawl.", exc)
        if since is None:
            logger.warning(
                "Incremental crawl requested but no watermark found; running "
                "a full crawl to establish one."
            )

    shards = generate_shards(min_stars=args.min_stars)
    if since is not None:
        shards = apply_pushed_since(shards, since)
        logger.info(
            "Incremental crawl: only repos pushed since %s.", since.date().isoformat()
        )
    else:
        logger.info("Full crawl (no incremental filter).")

    queue: asyncio.Queue = asyncio.Queue()
    for shard in shards:
        queue.put_nowait(shard)

    logger.info("Generated %d shards (min_stars=%d)", len(shards), args.min_stars)

    stats = CrawlStats(shards_total=len(shards))
    limiter = RateLimiter(name="graphql")

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

        interrupted = stop_task in done
        if interrupted:
            logger.warning("Shutdown signal received; cancelling workers.")
            for w in workers:
                w.cancel()
        else:
            stop_task.cancel()

        results = await asyncio.gather(*workers, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "[w%d] crashed with %s: %s",
                    i,
                    type(result).__name__,
                    result,
                )

        # Advance the watermark only on a clean, uninterrupted finish, so a
        # run killed mid-way is retried from the same point next time. A full
        # crawl also sets it, which is how the first run bootstraps the
        # watermark that later incremental runs read (ADR 0015).
        if not interrupted:
            try:
                await set_last_crawl_at(pool, run_started_at)
                logger.info("Crawl watermark set to %s.", run_started_at.isoformat())
            except Exception as exc:
                logger.warning("Failed to update crawl watermark: %s", exc)
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
