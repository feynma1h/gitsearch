"""Indexer pipeline entry point.

Usage:
    DATABASE_URL=postgresql://... \\
    EMBEDDING_SERVICE_URL=http://localhost:8001 \\
        python -m pipeline.main --top-n 20000

Reads repos from Postgres that don't yet have an embedding for the current
model, builds a source document for each, sends batches to the embedding
service, and upserts the resulting vectors back into Postgres.

The pipeline is **resumable**: only repos without an embedding row for
the current model are processed, so a crash mid-run is safe — restart
and it picks up where it left off.
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

from .client import EmbeddingClient
from .config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_SERVICE_URL,
    DEFAULT_WORKERS,
    MODEL_NAME,
)
from .db import create_pool, fetch_pending_repos
from .worker import ProgressCounter, pipeline_worker

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed repos into pgvector.")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Concurrent batches in flight (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"Texts per HTTP call (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--top-n", type=int, default=20_000,
        help="Max repos to consider in this run, ordered by stars desc.",
    )
    parser.add_argument(
        "--deadline-seconds", type=int, default=None,
        help="Stop pulling new batches after this many seconds. In-flight "
             "batches finish; remaining repos are left for the next run "
             "(the pipeline is idempotent — it skips repos with existing "
             "embeddings). Used by the chunked refresh workflow to stay "
             "under the 6-hour Actions job limit.",
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
    service_url = os.environ.get("EMBEDDING_SERVICE_URL", DEFAULT_SERVICE_URL)
    pool = await create_pool()

    pending = await fetch_pending_repos(pool, MODEL_NAME, args.top_n)
    if not pending:
        logger.info("No pending repos. Nothing to do.")
        await pool.close()
        return

    logger.info(
        "Loaded %d pending repos for model=%s.", len(pending), MODEL_NAME,
    )

    queue: asyncio.Queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    progress = ProgressCounter(total=len(pending))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    deadline: Optional[float] = None
    if args.deadline_seconds is not None:
        deadline = time.monotonic() + args.deadline_seconds

    start = time.monotonic()

    try:
        async with aiohttp.ClientSession() as session:
            client = EmbeddingClient(session, service_url)
            workers = [
                asyncio.create_task(
                    pipeline_worker(
                        i, queue, client, pool, args.batch_size, progress, deadline,
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
                logger.warning("Shutdown signal; cancelling workers.")
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            else:
                stop_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
    finally:
        await pool.close()

    elapsed = time.monotonic() - start
    logger.info("Indexer finished in %.1fs", elapsed)


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
