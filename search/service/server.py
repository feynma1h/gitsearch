"""Search service.

A long-running FastAPI app that:
  1. Accepts a natural-language query + optional filters/weights.
  2. Embeds the query via the embedding service (ADRs 0007-0010).
  3. Runs a hybrid pgvector search (ADR 0013) with the HNSW index
     built by the indexer (ADR 0011).
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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import (
    DEFAULT_EMBEDDING_SERVICE_URL,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MODEL_NAME,
)
from .db import SearchFilters, SearchHit, create_pool, search
from .embedding_client import EmbeddingClient, EmbeddingServiceError
from .ranking import (
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    DEFAULT_SIMILARITY_WEIGHT,
    DEFAULT_STARS_WEIGHT,
    DEFAULT_RECENCY_WEIGHT,
    ScoringWeights,
)

logger = logging.getLogger(__name__)


# Module-level state, populated by the lifespan handler.
_pool: Optional[asyncpg.Pool] = None
_http: Optional[aiohttp.ClientSession] = None
_embedder: Optional[EmbeddingClient] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Acquire shared resources at startup, release at shutdown.

    Both the DB pool and the aiohttp session are expensive to create
    per-request; we hold one of each for the life of the process.
    """
    global _pool, _http, _embedder

    service_url = os.environ.get(
        "EMBEDDING_SERVICE_URL", DEFAULT_EMBEDDING_SERVICE_URL,
    )

    _pool = await create_pool()
    _http = aiohttp.ClientSession()
    _embedder = EmbeddingClient(_http, service_url)

    logger.info(
        "Search service ready; model=%s, embedding service=%s",
        MODEL_NAME, service_url,
    )

    yield

    # Order matters: close HTTP first (in-flight requests may still need
    # the pool when fast-shutting-down), then the pool.
    if _http is not None:
        await _http.close()
    if _pool is not None:
        await _pool.close()
    _pool = _http = _embedder = None


app = FastAPI(title="Search Service", lifespan=lifespan)

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
    others fall back to defaults. See ADR 0013.
    """
    similarity: float = Field(default=DEFAULT_SIMILARITY_WEIGHT, ge=0)
    stars: float = Field(default=DEFAULT_STARS_WEIGHT, ge=0)
    recency: float = Field(default=DEFAULT_RECENCY_WEIGHT, ge=0)
    half_life_days: float = Field(default=DEFAULT_RECENCY_HALF_LIFE_DAYS, gt=0)


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
    similarity: float
    hybrid_score: float


class SearchResponse(BaseModel):
    hits: List[HitModel]
    model: str
    took_ms: int


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
async def do_search(req: SearchRequest) -> SearchResponse:
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

    hits = await search(
        _pool,
        query_vector=query_vec,
        filters=filters,
        weights=weights,
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
        hybrid_score=hit.hybrid_score,
    )
