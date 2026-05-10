"""Tests for ``pipeline_worker``.

We exercise the worker against fakes for the embedding client and the
DB upsert function — no network, no Postgres. The cases we care about:

  * the worker drains the queue and returns when empty
  * the worker honours an absolute monotonic deadline and stops cleanly
    *before* pulling a new batch (in-flight work is not interrupted)
  * a batch that fails with ``EmbeddingServiceError`` is re-queued, not
    dropped, and the worker exits so the orchestrator can react
  * the deadline is checked even when no work is pending (the worker
    still terminates, doesn't spin)

These tests pin down the behaviour the chunked GitHub Actions workflow
depends on: idempotent stop, no work loss, no orphaned in-flight calls.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from pipeline import worker as worker_module
from pipeline.client import EmbeddingServiceError
from pipeline.config import EMBEDDING_DIM
from pipeline.document_builder import RepoForEmbedding
from pipeline.worker import pipeline_worker


def _make_repo(name: str) -> RepoForEmbedding:
    return RepoForEmbedding(
        full_name=name,
        description=f"desc for {name}",
        primary_language="Python",
        topics=["test"],
        readme=f"# {name}\nA repo.",
    )


def _make_queue(n: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    for i in range(n):
        q.put_nowait((f"id-{i}", _make_repo(f"owner/repo-{i}")))
    return q


class _FakeClient:
    """Stand-in for EmbeddingClient that returns zero-vectors instantly.

    Tracks how many batches it was called with so tests can assert on
    dispatch behaviour without wiring up real HTTP.
    """

    def __init__(self, fail_after: Optional[int] = None) -> None:
        self.batches_seen: List[int] = []
        self.fail_after = fail_after  # raise EmbeddingServiceError after N calls

    async def embed(self, texts: List[str]) -> List[List[float]]:
        self.batches_seen.append(len(texts))
        if self.fail_after is not None and len(self.batches_seen) > self.fail_after:
            raise EmbeddingServiceError("simulated service failure")
        return [[0.0] * EMBEDDING_DIM for _ in texts]


@pytest.fixture
def stub_upsert(monkeypatch):
    """Replace upsert_embeddings with a no-op AsyncMock."""
    fake = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_module, "upsert_embeddings", fake)
    return fake


async def test_worker_drains_queue_and_returns(stub_upsert):
    """With no deadline, the worker processes everything and exits."""
    queue = _make_queue(7)
    client = _FakeClient()

    await pipeline_worker(
        worker_id=0, queue=queue, client=client, pool=object(),
        batch_size=3,
    )

    # 7 items at batch_size=3 → batches of 3, 3, 1.
    assert client.batches_seen == [3, 3, 1]
    assert queue.empty()
    assert stub_upsert.await_count == 3


async def test_worker_stops_at_deadline_before_next_batch(stub_upsert):
    """Deadline in the past → worker exits without pulling any batch.

    This is the load-bearing assertion: ``time.monotonic() > deadline``
    is checked at the *top* of the loop, before any work is pulled.
    """
    queue = _make_queue(10)
    client = _FakeClient()
    deadline_in_past = time.monotonic() - 1.0

    await pipeline_worker(
        worker_id=0, queue=queue, client=client, pool=object(),
        batch_size=3, deadline=deadline_in_past,
    )

    assert client.batches_seen == []  # no batches dispatched
    assert queue.qsize() == 10        # queue untouched
    assert stub_upsert.await_count == 0


async def test_worker_finishes_inflight_batch_then_stops(stub_upsert, monkeypatch):
    """A deadline that elapses *during* a batch must not interrupt that
    batch — the worker only checks the deadline between iterations."""
    queue = _make_queue(20)
    client = _FakeClient()

    # Set deadline to "now plus a hair" and have the embed() call burn
    # past it, so the second loop iteration sees an expired deadline.
    deadline = time.monotonic() + 0.05

    original_embed = client.embed

    async def slow_embed(texts):
        result = await original_embed(texts)
        # Simulate a batch that takes longer than the remaining deadline.
        await asyncio.sleep(0.1)
        return result

    client.embed = slow_embed  # type: ignore[assignment]

    await pipeline_worker(
        worker_id=0, queue=queue, client=client, pool=object(),
        batch_size=5, deadline=deadline,
    )

    # Exactly one batch should have completed: the one already in flight
    # when the deadline expired. The rest is left for the next chunk.
    assert client.batches_seen == [5]
    assert queue.qsize() == 15
    assert stub_upsert.await_count == 1


async def test_worker_returns_immediately_on_empty_queue(stub_upsert):
    """Empty queue plus no deadline → return without spinning."""
    queue: asyncio.Queue = asyncio.Queue()
    client = _FakeClient()

    await asyncio.wait_for(
        pipeline_worker(
            worker_id=0, queue=queue, client=client, pool=object(),
            batch_size=3,
        ),
        timeout=1.0,
    )

    assert client.batches_seen == []


async def test_worker_returns_immediately_on_empty_queue_with_deadline(stub_upsert):
    """Empty queue plus a future deadline → return without sleeping until
    the deadline. The empty-queue check is what terminates the worker."""
    queue: asyncio.Queue = asyncio.Queue()
    client = _FakeClient()
    far_future = time.monotonic() + 3600.0

    await asyncio.wait_for(
        pipeline_worker(
            worker_id=0, queue=queue, client=client, pool=object(),
            batch_size=3, deadline=far_future,
        ),
        timeout=1.0,  # would hang for an hour if deadline were the only exit
    )


async def test_service_error_requeues_batch_and_stops(stub_upsert):
    """When the embedding service fails, the failed batch goes back on
    the queue (so the next run can retry) and the worker exits — there's
    no point hammering a broken service."""
    queue = _make_queue(6)
    client = _FakeClient(fail_after=1)  # first batch succeeds, second raises

    await pipeline_worker(
        worker_id=0, queue=queue, client=client, pool=object(),
        batch_size=3,
    )

    # First batch (3) succeeded and was upserted; second batch (3) raised
    # and was re-queued. Worker exits without touching the rest.
    assert client.batches_seen == [3, 3]
    assert stub_upsert.await_count == 1  # only the successful batch was upserted
    assert queue.qsize() == 3            # the failed batch is back on the queue


async def test_unexpected_exception_does_not_kill_worker(stub_upsert):
    """A generic exception on one batch is logged and the worker keeps
    going. This is the existing 'except Exception' contract — pinning it
    down so a refactor doesn't accidentally tighten it."""
    queue = _make_queue(6)

    class FlakyClient(_FakeClient):
        async def embed(self, texts):
            self.batches_seen.append(len(texts))
            if len(self.batches_seen) == 1:
                raise RuntimeError("transient parse error")
            return [[0.0] * EMBEDDING_DIM for _ in texts]

    client = FlakyClient()

    await pipeline_worker(
        worker_id=0, queue=queue, client=client, pool=object(),
        batch_size=3,
    )

    assert client.batches_seen == [3, 3]   # both batches attempted
    assert stub_upsert.await_count == 1    # only the second succeeded
    assert queue.empty()                   # first batch's items are NOT re-queued
                                           # (this is the documented behaviour;
                                           # only EmbeddingServiceError re-queues)