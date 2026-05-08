"""README fetching pass entry point.

Usage:
    GITHUB_TOKEN=ghp_xxx DATABASE_URL=postgresql://... \\
        python -m src.readme_pass --top-n 20000

This is a separate program from the metadata crawler (``src.main``) because:
  - It hits a different rate-limit budget (REST, not GraphQL).
  - It is much slower per-token (5000 req/hour ceiling, one req per repo).
  - It is run after the metadata crawl, possibly multiple times as the
    corpus expands.

The pass is **resumable**: it only fetches repos where ``readme_fetched_at``
is NULL, so a crash mid-run is safe — restart and it picks up where it
left off. The work order is by stars descending, so partial runs capture
the most relevant repos first.
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

from .config import DEFAULT_README_TOP_N, DEFAULT_README_WORKERS
from .db import create_pool, fetch_pending_readmes
from .rate_limiter import RateLimiter
from .readme_client import ReadmeClient
from .readme_worker import ProgressCounter, readme_worker

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch READMEs for repos already in the database.",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_README_WORKERS,
        help=f"Concurrent workers (default: {DEFAULT_README_WORKERS}). "
             f"REST is cheaper per-request than GraphQL so we run more "
             f"workers than the metadata crawler.",
    )
    parser.add_argument(
        "--top-n", type=int, default=DEFAULT_README_TOP_N,
        help=f"Maximum number of repos (by stars desc) to consider in this "
             f"run. Default {DEFAULT_README_TOP_N:,} is a 'demo-quality' "
             f"target; raise to 100K+ for full coverage.",
    )
    parser.add_argument(
        "--deadline-seconds", type=int, default=None,
        help="Optional wall-clock cap; workers exit cleanly past this.",
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

    pool = await create_pool()

    pending = await fetch_pending_readmes(pool, args.top_n)
    if not pending:
        logger.info("No pending repos. Nothing to do.")
        await pool.close()
        return

    logger.info(
        "Loaded %d pending repos (top %d by stars).", len(pending), args.top_n,
    )

    queue: asyncio.Queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    limiter = RateLimiter(name="rest")
    progress = ProgressCounter(total=len(pending))

    deadline: Optional[float] = None
    if args.deadline_seconds is not None:
        deadline = time.monotonic() + args.deadline_seconds

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    connector = aiohttp.TCPConnector(limit=args.workers * 2)
    start = time.monotonic()

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            client = ReadmeClient(session, token, limiter)
            workers = [
                asyncio.create_task(
                    readme_worker(
                        i, queue, limiter, client, pool, deadline, progress,
                    )
                )
                for i in range(args.workers)
            ]
            stop_task = asyncio.create_task(stop_event.wait())

            done, _ = await asyncio.wait(
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
    logger.info("README pass finished in %.1fs", elapsed)


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
