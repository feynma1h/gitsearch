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


# ---------------------------------------------------------------------------
# Usage guides (ADR 0016)
# ---------------------------------------------------------------------------

# The "how do I use this?" guide is generated once per repo, lazily, from the
# repo's README and cached in `repository_guides`. Haiku is deliberately
# chosen over a larger model: the task is short-form summarisation of text we
# already have, so a small, cheap, fast model is the right fit (~$0.0065 per
# repo, paid only on the first click). Requires ANTHROPIC_API_KEY on the
# search service; if unset, the /guide endpoint is disabled.
GUIDE_MODEL: str = "claude-haiku-4-5"

# Short output: five terse sections. Keeps latency and cost down.
GUIDE_MAX_TOKENS: int = 800

# READMEs can be huge; the useful install/run material is almost always near
# the top. Truncate to bound input tokens.
GUIDE_README_CHAR_LIMIT: int = 12_000

# Each cache miss costs an LLM call, so throttle harder than /search.
GUIDE_RATE_LIMIT: str = "10/minute"

# --- Full-repo exploration for guides (ADR 0017) ----------------------------
# When GITHUB_TOKEN is set on the service, the guide model explores the live
# repository through a bounded tool loop (list_files / read_file) instead of
# relying on the README alone. These bounds cap the worst case per guide at
# GUIDE_MAX_TOOL_ROUNDS+1 model calls and a few tens of KB of fetched text;
# without the token the generator falls back to the README-only path.
GUIDE_MAX_TOOL_ROUNDS: int = 8       # model<->tool round-trips before the answer is forced
GUIDE_TREE_MAX_ENTRIES: int = 500    # file paths shown per listing
GUIDE_FILE_CHAR_LIMIT: int = 20_000  # characters returned per file read
