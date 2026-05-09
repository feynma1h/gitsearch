"""Centralised configuration for the search service.

Same layering as the rest of the project:
  - Constants here are tweakable defaults referenced from multiple files.
  - CLI / env vars override these per-run / per-environment.
  - Per-request overrides (e.g. ``weights`` in the search payload) take
    final precedence.

See ./docs/decisions/ for the rationale behind each value.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Embedding model — must match what the embedding service is serving.
# ---------------------------------------------------------------------------

# This needs to stay in lockstep with ``indexer/pipeline/config.MODEL_NAME``
# and the ``EMBEDDING_MODEL`` env var on the embedding service. The search
# API embeds the user query at request time and joins against the
# ``repository_embeddings`` table where ``model_name = MODEL_NAME``; if
# they don't match, you get zero results.
MODEL_NAME: str = "BAAI/bge-small-en-v1.5"

# Vector dimension; baked into the SQL schema (vector(384)). See ADR 0007.
EMBEDDING_DIM: int = 384


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

# How many candidates to fetch from pgvector before re-ranking by hybrid
# score. HNSW returns top-K by similarity *only*; the eventual top-N by
# hybrid score may include items that weren't in the top-N by similarity.
# Over-fetching gives the re-ranker enough room to surface them. See
# ADR 0013 for the rationale and the "what would change this" criteria.
DEFAULT_OVERFETCH_MULTIPLIER: int = 5
DEFAULT_OVERFETCH_MIN: int = 50
DEFAULT_OVERFETCH_MAX: int = 500


# ---------------------------------------------------------------------------
# pgvector / HNSW
# ---------------------------------------------------------------------------

# ``hnsw.ef_search`` controls how many candidates HNSW visits during graph
# traversal — higher = better recall, slightly higher latency. Default in
# pgvector is 40. We bump it modestly because we then re-rank, which
# benefits from a slightly larger candidate pool. Set per-connection
# inside the search query, not globally.
HNSW_EF_SEARCH: int = 100


# ---------------------------------------------------------------------------
# Embedding service client
# ---------------------------------------------------------------------------

DEFAULT_EMBEDDING_SERVICE_URL: str = "http://localhost:8001"

# Search is latency-sensitive in a way the indexer pipeline isn't, but
# the embedding service is also scale-to-zero in production — the first
# request after a few minutes of idle pays a ~10-15s cold start to load
# the model. We accept that latency on the cold path; warm requests
# still complete in ~25-40ms.
EMBEDDING_TIMEOUT_SECONDS: float = 30.0

# One retry is enough — if the cold start hasn't completed in 30s,
# something else is wrong and waiting longer won't help.
EMBEDDING_MAX_RETRIES: int = 1
EMBEDDING_RETRY_BACKOFF_SECONDS: float = 0.2


# ---------------------------------------------------------------------------
# Search defaults
# ---------------------------------------------------------------------------

DEFAULT_LIMIT: int = 10
MAX_LIMIT: int = 100
