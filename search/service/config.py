"""Centralised configuration for the search service.

Same layering as the rest of the project:
  - Constants here are tweakable defaults referenced from multiple files.
  - CLI / env vars override these per-run / per-environment.
  - Per-request overrides (e.g. ``weights`` in the search payload) take
    final precedence.

See ./docs/decisions/ for the rationale behind each value.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Embedding model — must match what the embedding service is serving.
# ---------------------------------------------------------------------------

# The *storage label* this service serves dense retrieval from: the
# ``model_name`` key in ``repository_embeddings`` (ADR 0006 keys rows by
# (repo_id, model_name) precisely so labels can sit side by side). The
# label names the encoder PLUS the document construction — phase 2
# stores enrichment-aware vectors under
# "BAAI/bge-small-en-v1.5+enrich-v1" beside the originals, and serving
# flips between them with the EMBEDDINGS_MODEL_LABEL env var. Rollback
# is the same env var pointed back; no recompute, no redeploy of code.
#
# The query-side encoder does NOT change with the label: the embedding
# service always runs bge-small (its own EMBEDDING_MODEL env), and every
# "+enrich-vN" label must be produced by that same encoder over an
# enriched document, or query and document vectors stop sharing a space.
MODEL_NAME: str = os.getenv("EMBEDDINGS_MODEL_LABEL", "BAAI/bge-small-en-v1.5")

# The encoder the embedding service actually runs — the label minus any
# "+doc-construction" suffix. Query embedding requests THIS (the
# service validates it against its loaded model); the full label is
# only ever a repository_embeddings key.
ENCODER_NAME: str = MODEL_NAME.split("+", 1)[0]

# Vector dimension; baked into the SQL schema (vector(384)). See ADR 0007.
EMBEDDING_DIM: int = 384

# ---------------------------------------------------------------------------
# Phase-2 retrieval (enrichment lane + name normalisation; ADR 0020)
# ---------------------------------------------------------------------------

# Master switch for the phase-2 retrieval additions: the enrichment
# arm of the FTS lane (repository_enrichment), the punctuation-
# normalised exact-name rule, and the criticality term's join
# (repository_signals). "off" serves the exact phase-1 statement —
# that's the rollback lever if the phase-2 tables are ever dropped
# (their absence with the flag on would error every query), and the
# verification lever for no-behavior-change infra deploys. With the
# flag ON and the tables merely *empty*, behaviour is still exactly
# phase-1, repo by repo — absence of enrichment degrades to today.
PHASE2_RETRIEVAL: bool = os.getenv(
    "PHASE2_RETRIEVAL", "on"
).strip().lower() not in ("off", "0", "false", "no")


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

# The FTS lane orders matches by (term coverage, stars): primary key =
# how many distinct query terms appear in the repo's light fields
# (name, topics, language, description — the README-free tsvector),
# secondary = star count within each coverage tier. This is GitHub's
# own ordering insight ("show popular repos before a random match in a
# long-forgotten repository") applied per coverage class: cover-density
# ranking measurably failed here (it put pytorch at rank ~1800 for
# "machine learning framework python" because repos *named* like the
# query win on density). A topics-only coverage variant also failed —
# see coverage_slots() in db.py. Popularity never gates recall: it
# orders within the lane, membership stays purely lexical (any two
# terms in the light fields, or a full websearch match incl. README).
# Coverage is computed against this many pre-bound single-lexeme
# slots; longer queries count only their first 8 content lexemes.
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
#
# Env-overridable since phase 2: with enrichment on, k becomes a
# precision↔canon-recall dial (ADR 0020 measured k=20 at canary +0.157
# / nDCG −0.011 vs k=50 at +0.091 / +0.018 under enriched embeddings),
# so the serving default can move with the enrichment flags in one
# config change.
RRF_K: int = int(os.getenv("SEARCH_RRF_K", "20"))
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

# Criticality: sat(dependents) over deps.dev dependent counts
# (repository_signals, migration 0010) — "how many published packages
# depend on this" is the authority signal stars can't fake (ADR 0018
# anticipated it; ADR 0020 wires it). Same saturation form as stars but
# a FIXED pivot: the candidate-set geometric mean that stars use
# degenerates here because most candidates have no published package at
# all (NULL -> the term contributes 0, deliberately distinct from
# "published but unused" = 0 dependents, which also scores 0).
#
# The default weight is 0.0 — the term ships dark. The eval sweeps it
# per-request (weights.criticality); the default only moves after a
# measured win, so deploying this code changes nothing by itself.
DEFAULT_CRITICALITY_WEIGHT: float = 0.0
DEPENDENTS_PIVOT: float = 1_000.0


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
