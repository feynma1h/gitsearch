"""HTTP client for the embedding service.

The service is a separate process (FastAPI app) that loads the model
once and exposes ``POST /embed`` accepting a batch of texts and
returning a batch of vectors. ADR 0005 covers why the model runs as
a long-lived service rather than in-process; ADR 0009 covers the
HTTP+JSON transport; ADR 0010 covers why batching is caller-side.

This client is intentionally minimal: send a list of strings, get back a
list of vectors. Retries transient errors. Raises on persistent ones.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

import aiohttp

from .config import EMBEDDING_DIM, ENCODER_NAME, SERVICE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


class EmbeddingServiceError(Exception):
    """Non-retryable error from the embedding service."""


class EmbeddingClient:
    """Async client for the embedding service.

    One instance is shared across all pipeline workers. ``aiohttp.ClientSession``
    handles connection pooling internally.
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts. Returns vectors in the same order."""
        if not texts:
            return []

        url = f"{self._base_url}/embed"
        # ENCODER_NAME, not MODEL_NAME: storage labels like
        # "...+enrich-v1" name a document construction over the same
        # encoder; the service checks the requested model against what
        # it loaded and must keep doing so.
        payload = {"texts": texts, "model": ENCODER_NAME}

        last_error: str = ""
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=SERVICE_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status in _RETRY_STATUSES:
                        last_error = f"HTTP {resp.status}"
                        await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))
                        continue

                    if resp.status != 200:
                        body = await resp.text()
                        raise EmbeddingServiceError(
                            f"HTTP {resp.status}: {body[:300]}"
                        )

                    data = await resp.json()
                    embeddings = data["embeddings"]
                    self._validate(embeddings, len(texts))
                    return embeddings

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))

        raise EmbeddingServiceError(
            f"Exhausted {_MAX_RETRIES} retries; last error: {last_error}"
        )

    @staticmethod
    def _validate(embeddings: List[List[float]], expected_count: int) -> None:
        """Sanity-check the response shape so bad outputs fail loudly."""
        if len(embeddings) != expected_count:
            raise EmbeddingServiceError(
                f"Expected {expected_count} embeddings, got {len(embeddings)}"
            )
        for i, emb in enumerate(embeddings):
            if len(emb) != EMBEDDING_DIM:
                raise EmbeddingServiceError(
                    f"Embedding {i} has dim {len(emb)}, expected {EMBEDDING_DIM}"
                )
