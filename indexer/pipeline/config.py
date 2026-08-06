"""Centralised configuration for the indexer pipeline.

Same layering principles as the crawler:
  - Constants here are tweakable defaults referenced from multiple files.
  - CLI flags override these per-run.
  - Env vars (DATABASE_URL, EMBEDDING_SERVICE_URL) handle per-environment.

See ../docs/decisions/ for the rationale behind each value.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

# The *storage label* written to repository_embeddings.model_name (ADR
# 0006 keys rows by (repo_id, model_name) so labels sit side by side).
# The default names the raw encoder; a label containing "+enrich"
# (e.g. "BAAI/bge-small-en-v1.5+enrich-v1") means "the same encoder
# over enrichment-aware documents" — the pipeline then folds
# repository_enrichment text into each source document (ADR 0020) and
# records vectors under the versioned label, leaving the originals
# untouched. Serving flips between labels via the search service's own
# EMBEDDINGS_MODEL_LABEL; rollback is that config change.
#
# The encoder itself NEVER changes with the label — the embedding
# service loads its model from its own EMBEDDING_MODEL env var, and
# query vectors must share the document vectors' space.
MODEL_NAME: str = os.getenv("EMBEDDINGS_MODEL_LABEL", "BAAI/bge-small-en-v1.5")

# Labels carrying this marker embed enrichment-aware documents.
ENRICH_LABEL_MARKER: str = "+enrich"
INCLUDE_ENRICHMENT: bool = ENRICH_LABEL_MARKER in MODEL_NAME

# The encoder the embedding service must actually run: the label minus
# any "+suffix" (labels follow "encoder+doc-construction"). This is
# what the HTTP client requests — the service serves encoders and
# rightly rejects storage labels it has never heard of; the full label
# exists only in repository_embeddings.model_name.
ENCODER_NAME: str = MODEL_NAME.split("+", 1)[0]

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

# Enrichment text is prepended ahead of the README (signal-rich fields
# first, ADR 0008), so inside the model's ~2000-char window it competes
# with README head. Aliases/categories/queries lines are short; the
# mined description block is the one that can run long (up to 1,200
# stored chars), so it gets its own cap to leave the README real room.
ENRICHMENT_DESC_MAX_CHARS: int = 600

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

# Default URL for the embedding service. Override via EMBEDDING_SERVICE_URL.
DEFAULT_SERVICE_URL: str = "http://localhost:8001"

# Per-request timeout (seconds). The model can be slow on the first call
# (lazy load) but should be quick after that.
SERVICE_TIMEOUT_SECONDS: float = 60.0
