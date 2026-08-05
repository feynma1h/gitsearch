"""Search service.

A long-running FastAPI app that:
  1. Accepts a natural-language query + optional filters/weights.
  2. Embeds the query via the embedding service (ADRs 0007-0010).
  3. Runs the three-lane hybrid retrieval (ADR 0018): full-text +
     dense (HNSW, ADR 0011) + name lanes, fused and blended in one
     SQL statement.
  4. Returns ranked results.

Run with:
    uvicorn service.server:app --host 0.0.0.0 --port 8002

Endpoints:
    GET  /health
    POST /search   { "query": "...", "limit"?: N, "filters"?: {...}, "weights"?: {...} }
                   -> { "hits": [...], "model": "...", "took_ms": N }
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import aiohttp
import asyncpg
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import (
    DEFAULT_EMBEDDING_SERVICE_URL,
    DEFAULT_LIMIT,
    GUIDE_MODEL,
    GUIDE_RATE_LIMIT,
    MAX_LIMIT,
    MODEL_NAME,
)
from .db import (
    CachedGuide,
    RepoForGuide,
    SearchFilters,
    SearchHit,
    create_pool,
    fetch_repo_for_guide,
    get_cached_guide,
    search,
    upsert_guide,
)
from .embedding_client import EmbeddingClient, EmbeddingServiceError
from .guide import GuideGenerationError, generate_guide
from .repo_browser import RepoBrowser
from .config import (
    FULL_TEXT_WEIGHT,
    NAME_WEIGHT,
    RRF_K,
    SEMANTIC_WEIGHT,
)
from .ranking import (
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    DEFAULT_SIMILARITY_WEIGHT,
    DEFAULT_STARS_WEIGHT,
    DEFAULT_RECENCY_WEIGHT,
    LaneWeights,
    ScoringWeights,
)

# uvicorn only configures handlers for its own loggers; the root logger
# gets none, so app records (the startup line, guide warnings) would
# otherwise vanish — on Cloud Run they'd never reach Cloud Logging.
# basicConfig routes them to stderr; it is a no-op if the root logger
# is already configured.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


# Module-level state, populated by the lifespan handler.
_pool: Optional[asyncpg.Pool] = None
_http: Optional[aiohttp.ClientSession] = None
_embedder: Optional[EmbeddingClient] = None
# Anthropic client for usage guides. Stays None when ANTHROPIC_API_KEY is
# unset, which disables the /guide endpoint rather than failing at startup.
_anthropic: Optional[AsyncAnthropic] = None
# GitHub token for the guide's repo-exploration tools (ADR 0017). When unset,
# guides fall back to the README-only path rather than failing.
_github_token: Optional[str] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Acquire shared resources at startup, release at shutdown.

    The DB pool, the aiohttp session, and the Anthropic client are all
    expensive to create per-request; we hold one of each for the life of
    the process.
    """
    global _pool, _http, _embedder, _anthropic, _github_token

    service_url = os.environ.get(
        "EMBEDDING_SERVICE_URL", DEFAULT_EMBEDDING_SERVICE_URL,
    )

    _pool = await create_pool()
    _http = aiohttp.ClientSession()
    _embedder = EmbeddingClient(_http, service_url)

    if os.environ.get("ANTHROPIC_API_KEY"):
        _anthropic = AsyncAnthropic()
    else:
        _anthropic = None
        logger.warning(
            "ANTHROPIC_API_KEY not set; the /guide endpoint is disabled."
        )

    _github_token = os.environ.get("GITHUB_TOKEN") or None
    if _anthropic and not _github_token:
        logger.warning(
            "GITHUB_TOKEN not set; guides use the stored README only "
            "(no repository exploration)."
        )

    guides_mode = "off"
    if _anthropic:
        guides_mode = "full-repo" if _github_token else "readme-only"
    logger.info(
        "Search service ready; model=%s, embedding service=%s, guides=%s",
        MODEL_NAME, service_url, guides_mode,
    )

    yield

    # Order matters: close HTTP first (in-flight requests may still need
    # the pool when fast-shutting-down), then the pool.
    if _http is not None:
        await _http.close()
    if _anthropic is not None:
        await _anthropic.close()
    if _pool is not None:
        await _pool.close()
    _pool = _http = _embedder = _anthropic = None


app = FastAPI(title="Search Service", lifespan=lifespan)

# Rate limiting. Per-IP throttle on /search keeps one bad actor from
# burning embedding-service capacity or Supabase row reads. Health
# checks are intentionally not throttled so Cloud Run's health checks
# never trip the limit.
#
# get_remote_address pulls the client IP from request.client.host;
# behind Cloud Run's proxy the real IP is in X-Forwarded-For, which
# slowapi's default extractor handles correctly. The 30/minute default
# is generous for human use (two searches every four seconds is
# already aggressive typing) and tight enough that a script burst
# gets throttled within a second. SEARCH_RATE_LIMIT exists so a local
# instance can serve the eval harness's sweeps at full speed; leave it
# unset in production.
_search_rate_limit = os.environ.get("SEARCH_RATE_LIMIT", "30/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[_search_rate_limit])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — let the browser talk to us from a separate frontend origin.
# In dev, frontends run on localhost:3000, :5173, :8000, etc. In
# production, the frontend's deployed origin should be the only entry
# in the allow-list — set ALLOWED_ORIGINS=https://your-domain.com to
# narrow it down. The default below is permissive for local dev only.
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "*")
_allowed_origins = (
    ["*"] if _allowed_origins_env == "*"
    else [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class FiltersModel(BaseModel):
    """Optional filters. None / empty fields are no-ops."""
    language: Optional[str] = None
    topics: Optional[List[str]] = None
    min_stars: Optional[int] = Field(default=None, ge=0)
    exclude_archived: bool = True


class WeightsModel(BaseModel):
    """Optional per-request weight overrides for the hybrid score.

    Defaults match ``ranking.py``. Any subset can be overridden; the
    others fall back to defaults. ``similarity`` weights the fused
    relevance signal (the name predates hybrid retrieval and is kept
    for client compatibility — the frontend sliders send it).

    The last four knobs configure the RRF fusion itself. They exist for
    the eval harness's sweeps; interactive clients shouldn't need them.
    See ADR 0018.
    """
    similarity: float = Field(default=DEFAULT_SIMILARITY_WEIGHT, ge=0)
    stars: float = Field(default=DEFAULT_STARS_WEIGHT, ge=0)
    recency: float = Field(default=DEFAULT_RECENCY_WEIGHT, ge=0)
    half_life_days: float = Field(default=DEFAULT_RECENCY_HALF_LIFE_DAYS, gt=0)
    full_text_weight: float = Field(default=FULL_TEXT_WEIGHT, ge=0)
    semantic_weight: float = Field(default=SEMANTIC_WEIGHT, ge=0)
    name_weight: float = Field(default=NAME_WEIGHT, ge=0)
    rrf_k: int = Field(default=RRF_K, ge=1, le=1000)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    filters: FiltersModel = Field(default_factory=FiltersModel)
    weights: WeightsModel = Field(default_factory=WeightsModel)


class HitModel(BaseModel):
    repo_id: str
    full_name: str
    description: Optional[str]
    url: str
    primary_language: Optional[str]
    topics: List[str]
    stars: int
    pushed_at: Optional[str]  # ISO 8601 string for cleaner JSON
    similarity: float         # raw cosine similarity (0.0 if not embedded)
    # True when the query is exactly this repo's name (or owner/name) —
    # such hits sort above everything else, popularity-independent.
    exact_name: bool
    hybrid_score: float
    # Per-component contributions to hybrid_score (already weight-multiplied):
    # fused relevance, saturated stars, recency. Their sum equals
    # hybrid_score. Exposed so the UI can show why a result ranked where
    # it did. See ADR 0018.
    similarity_contribution: float
    stars_contribution: float
    recency_contribution: float


class SearchResponse(BaseModel):
    hits: List[HitModel]
    model: str
    took_ms: int


class GuideResponse(BaseModel):
    repo_id: str
    full_name: str
    guide: str          # GitHub-flavored Markdown, fixed five-section format
    model: str
    cached: bool        # True if served from repository_guides, False if freshly generated


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Cheap liveness check. Doesn't touch the DB or the embedding
    service; it just confirms the process is up and the resources were
    acquired at startup."""
    if _pool is None or _embedder is None:
        raise HTTPException(503, "service not ready")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/search", response_model=SearchResponse)
@limiter.limit(_search_rate_limit)
async def do_search(request: Request, req: SearchRequest) -> SearchResponse:
    if _pool is None or _embedder is None:
        raise HTTPException(503, "service not ready")

    started = time.monotonic()

    try:
        query_vec = await _embedder.embed_query(req.query)
    except EmbeddingServiceError as exc:
        logger.warning("Embedding failed: %s", exc)
        raise HTTPException(502, f"embedding service error: {exc}") from exc

    filters = SearchFilters(
        language=req.filters.language,
        topics=req.filters.topics,
        min_stars=req.filters.min_stars,
        exclude_archived=req.filters.exclude_archived,
    )
    weights = ScoringWeights(
        similarity=req.weights.similarity,
        stars=req.weights.stars,
        recency=req.weights.recency,
        half_life_days=req.weights.half_life_days,
    )
    lanes = LaneWeights(
        full_text=req.weights.full_text_weight,
        semantic=req.weights.semantic_weight,
        name=req.weights.name_weight,
        rrf_k=req.weights.rrf_k,
    )

    hits = await search(
        _pool,
        query=req.query,
        query_vector=query_vec,
        filters=filters,
        weights=weights,
        lanes=lanes,
        limit=req.limit,
    )

    took_ms = int((time.monotonic() - started) * 1000)
    return SearchResponse(
        hits=[_hit_to_model(h) for h in hits],
        model=MODEL_NAME,
        took_ms=took_ms,
    )


def _hit_to_model(hit: SearchHit) -> HitModel:
    return HitModel(
        repo_id=hit.repo_id,
        full_name=hit.full_name,
        description=hit.description,
        url=hit.url,
        primary_language=hit.primary_language,
        topics=hit.topics,
        stars=hit.stars,
        pushed_at=hit.pushed_at.isoformat() if hit.pushed_at is not None else None,
        similarity=hit.similarity,
        exact_name=hit.exact_name,
        hybrid_score=hit.hybrid_score,
        similarity_contribution=hit.similarity_contribution,
        stars_contribution=hit.stars_contribution,
        recency_contribution=hit.recency_contribution,
    )


def _guide_is_stale(cached: CachedGuide, repo: RepoForGuide) -> bool:
    """A cached guide is stale if the README has been re-fetched since it
    was generated. When there's nothing to compare (no current README, or a
    pre-tracking cache), err toward the safe/cheap side."""
    current = repo.readme_fetched_at
    cached_at = cached.source_readme_fetched_at
    if current is None:
        return False              # no README revision to compare against
    if cached_at is None:
        return True               # cache predates README tracking; refresh
    return current > cached_at


@app.get("/guide/{repo_id:path}", response_model=GuideResponse)
@limiter.limit(GUIDE_RATE_LIMIT)
async def get_guide(request: Request, repo_id: str) -> GuideResponse:
    """Return a short "how do I use this?" guide for one repo.

    Served from the `repository_guides` cache when present and fresh;
    otherwise generated once (Claude Haiku 4.5), stored, and returned.
    With GITHUB_TOKEN set the model explores the live repository while
    writing (ADR 0017); otherwise it works from the stored README (ADR
    0016). Requires ANTHROPIC_API_KEY on a cache miss.
    """
    if _pool is None:
        raise HTTPException(503, "service not ready")

    repo = await fetch_repo_for_guide(_pool, repo_id)
    if repo is None:
        raise HTTPException(404, "repository not found")

    cached = await get_cached_guide(_pool, repo_id)
    if cached is not None and not _guide_is_stale(cached, repo):
        return GuideResponse(
            repo_id=repo.repo_id,
            full_name=repo.full_name,
            guide=cached.guide,
            model=cached.model_name,
            cached=True,
        )

    if _anthropic is None:
        raise HTTPException(
            503, "usage guides are not configured (ANTHROPIC_API_KEY unset)"
        )

    browser = None
    if _github_token is not None and _http is not None:
        browser = RepoBrowser(_http, repo.full_name, _github_token)

    try:
        guide = await generate_guide(_anthropic, repo, browser)
    except GuideGenerationError as exc:
        logger.warning("Guide generation failed for %s: %s", repo_id, exc)
        raise HTTPException(502, f"guide generation error: {exc}") from exc

    await upsert_guide(_pool, repo_id, guide, GUIDE_MODEL, repo.readme_fetched_at)
    return GuideResponse(
        repo_id=repo.repo_id,
        full_name=repo.full_name,
        guide=guide,
        model=GUIDE_MODEL,
        cached=False,
    )
