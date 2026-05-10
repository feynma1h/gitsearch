"""Pipeline worker: pulls repos from a queue, embeds them in batches,
and writes results to Postgres.

The pipeline is structured around a queue of pending repos, populated
once at startup. Workers drain the queue in batches, sending one HTTP
call per batch to the embedding service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

import asyncpg

from .client import EmbeddingClient, EmbeddingServiceError
from .config import DEFAULT_BATCH_SIZE, MODEL_NAME
from .db import upsert_embeddings
from .document_builder import RepoForEmbedding, build_source_text, source_hash

logger = logging.getLogger(__name__)


async def pipeline_worker(
    worker_id: int,
    queue: asyncio.Queue,
    client: EmbeddingClient,
    pool: asyncpg.Pool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: Optional["ProgressCounter"] = None,
    deadline: Optional[float] = None,
) -> None:
    """Drain the queue in batches until empty or deadline reached.

    ``deadline`` is an absolute ``time.monotonic()`` timestamp. When set,
    the worker checks it before pulling each batch and exits cleanly past
    that point — the in-flight batch (if any) finishes first, so no work
    is wasted. Used by the chunked refresh workflow to stay under the
    6-hour Actions job limit.
    """
    while True:
        if deadline is not None and time.monotonic() > deadline:
            logger.info("[w%d] deadline reached; exiting", worker_id)
            return
        batch = _collect_batch(queue, batch_size)
        if not batch:
            return

        try:
            await _process_batch(worker_id, batch, client, pool)
            if progress is not None:
                progress.tick(len(batch))
        except EmbeddingServiceError as exc:
            # If the service itself is broken, no point continuing.
            logger.error("[w%d] Embedding service error: %s", worker_id, exc)
            # Re-queue the batch so another run can pick it up.
            for item in batch:
                queue.put_nowait(item)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[w%d] Unexpected error on batch: %s", worker_id, exc)


def _collect_batch(
    queue: asyncio.Queue,
    batch_size: int,
) -> List[Tuple[str, RepoForEmbedding]]:
    """Pop up to ``batch_size`` items from the queue, non-blocking."""
    batch: List[Tuple[str, RepoForEmbedding]] = []
    for _ in range(batch_size):
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _process_batch(
    worker_id: int,
    batch: List[Tuple[str, RepoForEmbedding]],
    client: EmbeddingClient,
    pool: asyncpg.Pool,
) -> None:
    """Embed one batch and write the results."""
    texts = [build_source_text(repo) for _, repo in batch]
    hashes = [source_hash(text) for text in texts]

    embeddings = await client.embed(texts)

    rows = [
        (repo_id, MODEL_NAME, embedding, src_hash)
        for (repo_id, _), embedding, src_hash in zip(batch, embeddings, hashes)
    ]
    await upsert_embeddings(pool, rows)

    logger.debug("[w%d] embedded %d repos", worker_id, len(batch))


class ProgressCounter:
    """Lightweight progress counter for long indexing runs."""

    def __init__(self, total: int, log_every: int = 500) -> None:
        self._total = total
        self._done = 0
        self._log_every = log_every
        self._start = time.monotonic()

    def tick(self, n: int = 1) -> None:
        prev = self._done
        self._done += n
        # Log when we cross a `log_every` boundary.
        if (prev // self._log_every) != (self._done // self._log_every) or self._done == self._total:
            elapsed = time.monotonic() - self._start
            rate = self._done / elapsed if elapsed > 0 else 0
            remaining = self._total - self._done
            eta = remaining / rate if rate > 0 else float("inf")
            logger.info(
                "Progress: %d/%d (%.1f%%) | %.1f repos/s | ETA %.0fs",
                self._done, self._total,
                100 * self._done / self._total,
                rate, eta,
            )
