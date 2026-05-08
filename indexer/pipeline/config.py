"""Centralised configuration for the indexer pipeline.

Same layering principles as the crawler:
  - Constants here are tweakable defaults referenced from multiple files.
  - CLI flags override these per-run.
  - Env vars (DATABASE_URL, EMBEDDING_SERVICE_URL) handle per-environment.

See ../docs/decisions/ for the rationale behind each value.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

# The embedding model. Must match what the embedding service is loading.
# Used as the `model_name` in repository_embeddings rows so we can A/B test
# models side-by-side. See ADR 0006 (separate embeddings table) and ADR 0007
# (model choice).
MODEL_NAME: str = "BAAI/bge-small-en-v1.5"

# Vector dimension produced by the model. Hard-coded because it's also
# baked into the SQL schema (vector(384)). Changing the model usually means
# changing the dimension, which is a schema change, which is a new ADR.
EMBEDDING_DIM: int = 384

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

# How many texts to send per HTTP call to the embedding service.
#
# 32 is a sweet spot: large enough that fixed per-request overhead amortises
# nicely, small enough that one slow batch doesn't block other workers for
# long. See ADR 0010 (caller-side batching) for the API shape.
DEFAULT_BATCH_SIZE: int = 32

# Concurrent batches in flight. Total in-flight texts = WORKERS * BATCH_SIZE.
DEFAULT_WORKERS: int = 4

# Maximum characters of source text to embed per repo.
#
# bge-small has a 512-token context (~2000 chars). The model truncates
# internally, but capping client-side reduces network bytes. We cap a bit
# above the model's effective window to give it some room.
SOURCE_TEXT_MAX_CHARS: int = 2_500

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

# Default URL for the embedding service. Override via EMBEDDING_SERVICE_URL.
DEFAULT_SERVICE_URL: str = "http://localhost:8001"

# Per-request timeout (seconds). The model can be slow on the first call
# (lazy load) but should be quick after that.
SERVICE_TIMEOUT_SECONDS: float = 60.0
