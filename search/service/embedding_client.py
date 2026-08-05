"""HTTP client for the embedding service, tuned for search latency.

Structurally identical to ``indexer/pipeline/client.py`` — same service,
same wire format. Three deliberate differences:

  - Timeout sized to the frontend's request budget, which must cover
    the embedding service's full cold start (~38s measured).
  - Fewer retries (the indexer retries harder; here the browser has
    given up long before a third attempt could matter).
  - Always sends a one-element batch (the user query). ADR 0010
    anticipates this — it's a fine pattern at low QPS, and dynamic
    server-side batching is the documented next step if we ever need
    higher throughput.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

import aiohttp

from .config import (
    EMBEDDING_DIM,
    EMBEDDING_MAX_RETRIES,
    EMBEDDING_RETRY_BACKOFF_SECONDS,
    EMBEDDING_TIMEOUT_SECONDS,
    MODEL_NAME,
)

logger = logging.getLogger(__name__)

# 429 included: Cloud Run returns it when no instance can take the
# request yet (scale-from-zero); it deserves a retry, not an error.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class EmbeddingServiceError(Exception):
    """Non-retryable error from the embedding service."""


class EmbeddingClient:
    """Async client for the embedding service.

    One instance is shared across all incoming requests; ``aiohttp``
    handles connection pooling. See ADR 0009 for HTTP+JSON rationale.
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def embed_query(self, query: str) -> List[float]:
        """Embed a single user query. Returns a 384-dim vector."""
        if not query or not query.strip():
            raise ValueError("query must be non-empty")

        url = f"{self._base_url}/embed"
        # The embedding model (bge-small-en-v1.5) does not use distinct
        # query/passage prefixes — embed the raw text as-is. Asymmetric
        # models like e5 or bge-large *do* want a "query: " prefix; if
        # we ever swap to one, that change goes here.
        payload = {"texts": [query], "model": MODEL_NAME}

        last_error: str = ""
        for attempt in range(EMBEDDING_MAX_RETRIES + 1):
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=EMBEDDING_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status in _RETRY_STATUSES:
                        last_error = f"HTTP {resp.status}"
                        if attempt < EMBEDDING_MAX_RETRIES:
                            await asyncio.sleep(
                                EMBEDDING_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                            )
                            continue

                    if resp.status != 200:
                        body = await resp.text()
                        raise EmbeddingServiceError(
                            f"HTTP {resp.status}: {body[:300]}"
                        )

                    data = await resp.json()
                    embeddings = data["embeddings"]
                    self._validate(embeddings)
                    return embeddings[0]

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < EMBEDDING_MAX_RETRIES:
                    await asyncio.sleep(
                        EMBEDDING_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                    )

        raise EmbeddingServiceError(
            f"Exhausted {EMBEDDING_MAX_RETRIES + 1} attempts; "
            f"last error: {last_error}"
        )

    @staticmethod
    def _validate(embeddings: List[List[float]]) -> None:
        """Sanity-check the response shape."""
        if len(embeddings) != 1:
            raise EmbeddingServiceError(
                f"Expected 1 embedding, got {len(embeddings)}"
            )
        if len(embeddings[0]) != EMBEDDING_DIM:
            raise EmbeddingServiceError(
                f"Embedding has dim {len(embeddings[0])}, "
                f"expected {EMBEDDING_DIM}"
            )
