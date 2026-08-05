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
# Retrieval lanes (ADR 0018)
# ---------------------------------------------------------------------------

# Candidate generation runs three lanes in one SQL statement and fuses
# them with weighted Reciprocal Rank Fusion. Lane depths bound both
# latency and how far down each ranking the fusion can reach; RRF makes
# depth cheap (rank 180 contributes ~nothing at k=50), so 200/200/50 is
# headroom, not tuning.
FTS_LANE_LIMIT: int = 200     # weighted tsvector, websearch_to_tsquery
DENSE_LANE_LIMIT: int = 200   # halfvec HNSW KNN
NAME_LANE_LIMIT: int = 50     # pg_trgm exact/prefix/fuzzy on repo name

# The FTS lane orders matches by (curated-term coverage, stars):
# primary key = how many distinct query terms appear in the repo's
# TOPICS + PRIMARY LANGUAGE (tsvector weight B), secondary = star
# count within each coverage tier. This is GitHub's own ordering
# insight ("show popular repos before a random match in a
# long-forgotten repository") applied per coverage class. Two broader
# signals measurably failed here: cover-density ranking put pytorch at
# rank ~1800 for "machine learning framework python" (repos named
# like the query win), and counting coverage across name/description
# too let boilerplate descriptions overflow the lane's full-coverage
# tier (~350 repos "cover" that query somewhere; only 49 cover it in
# topics, and the canonical repos sit directly under them by stars).
# Popularity still never gates recall: it orders within the lane,
# membership stays purely lexical (any two terms in the light fields,
# or a full websearch match incl. README), and descriptions keep
# ranking weight through the dense lane, which embeds them. Coverage
# is computed against this many pre-bound single-lexeme slots; longer
# queries count only their first 8 content lexemes.
FTS_COVERAGE_SLOTS: int = 8
# The fuzzy trigram arm of the name lane only fires for queries of at
# most this many words: typos people type are short ("pytorhc"), and
# trigram-scanning a whole sentence against 267K names costs seconds
# while contributing nothing.
NAME_FUZZY_MAX_TOKENS: int = 2

# RRF: score = sum over lanes of weight / (k + rank). The phase-1 eval
# swept 20/50/60/200 on the 50-query canary suite: smaller k (sharper
# rank discounts) won monotonically — 0.527 / 0.484 / 0.469 / 0.415
# canary recall@10 — because sharp fusion amplifies the lanes'
# already-good top ordering, while flat fusion hands the decision to
# cross-lane agreement, which favors listicle-style repos. The eval
# harness can still sweep it per-request.
RRF_K: int = 20
FULL_TEXT_WEIGHT: float = 1.0
SEMANTIC_WEIGHT: float = 1.0
# The name lane mostly re-confirms repos the other lanes already found
# (and the exact-name rule handles the headline case), so it whispers
# rather than shouts.
NAME_WEIGHT: float = 0.5

# pg_trgm similarity threshold for the fuzzy name match (the `%`
# operator). 0.3 is the extension default, set explicitly per query so
# server config can't drift under us.
TRGM_SIMILARITY_THRESHOLD: float = 0.3

# Stars saturation: sat(stars) = stars / (stars + pivot), pivot = the
# geometric mean of the candidate set's star counts (Elastic's
# rank_feature heuristic), clamped to a sane band so degenerate
# candidate sets (all-tiny or all-megastar) can't flatten or explode
# the curve. Corpus floor is ~200 stars; 20K keeps 10K-vs-100K
# distinguishable without letting megastars run away.
STARS_PIVOT_MIN: float = 100.0
STARS_PIVOT_MAX: float = 20_000.0

# Recency is a bounded maintenance signal, not decay-to-zero: an old
# canonical library must not sink below the floor merely for being
# finished. A repo pushed today scores 1.0; the floor is the asymptote
# for abandoned ones (pushed_at NULL still scores 0 — "never pushed"
# is a different statement than "stable").
RECENCY_FLOOR: float = 0.25

# Demotions, applied as multipliers on the final blended score (every
# component is scaled, so the "components sum to the score" contract
# survives). Archived repos are usually filtered out entirely; the
# demotion covers the include-archived path. Forks stay searchable but
# shouldn't outrank their upstream.
DEMOTION_ARCHIVED: float = 0.5
DEMOTION_FORK: float = 0.8


# ---------------------------------------------------------------------------
# pgvector / HNSW
# ---------------------------------------------------------------------------

# ``hnsw.ef_search`` controls how many candidates HNSW visits during
# graph traversal — it caps how many rows the dense lane can return, so
# it must be >= DENSE_LANE_LIMIT. Set per-transaction in the search
# query, not globally. pgvector 0.8's iterative scans (relaxed_order)
# are enabled alongside it when available, so filtered searches keep
# scanning until the lane is full instead of coming back short.
HNSW_EF_SEARCH: int = 200


# ---------------------------------------------------------------------------
# Embedding service client
# ---------------------------------------------------------------------------

DEFAULT_EMBEDDING_SERVICE_URL: str = "http://localhost:8001"

# Search is latency-sensitive, but the embedding service is
# scale-to-zero in production and its cold start measures ~38s end to
# end (container spin-up, ~27s of imports, ~8s model load — observed
# 2026-08-05; the earlier 10-15s estimate undershot and made every
# first-search-after-idle fail at the timeout). 75s matches the
# frontend's own request budget; warm requests still complete in
# ~25-40ms, so a generous ceiling costs nothing on the happy path.
# NOTE: Cloud Run's service-level request timeout must exceed this —
# a 30s platform cap was silently 504-ing every cold search at 30.0s
# no matter what this value said (raised to 90s on 2026-08-05; future
# `gcloud run deploy` calls inherit it unless --timeout overrides).
EMBEDDING_TIMEOUT_SECONDS: float = 75.0

# One retry is enough — if two attempts within the budget haven't
# gotten through, the service is down rather than cold, and the
# frontend has already shown its error state.
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
