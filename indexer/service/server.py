"""Embedding service.

A long-running FastAPI app that loads the sentence-transformers model once
and exposes ``POST /embed`` to embed batches of texts.

Run with:
    uvicorn service.server:app --host 0.0.0.0 --port 8001

The model and dimension are configured via environment variables so the
same service binary can serve different models in different deployments
without code changes.

Endpoints:
    POST /embed   { "texts": [...], "model"?: "..." }  -> { "embeddings": [...] }
    GET  /health  -> { "status": "ok", "model": "..." }
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .model import EmbeddingModel

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "128"))


# Loaded at startup, replaced inside the lifespan context.
_model: Optional[EmbeddingModel] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Load the model at startup, release at shutdown."""
    global _model
    _model = EmbeddingModel(MODEL_NAME)
    logger.info("Service ready with model %s", MODEL_NAME)
    yield
    _model = None


app = FastAPI(title="Embedding Service", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1, max_length=MAX_BATCH_SIZE)
    # Optional: clients can pass the model they expect. If it doesn't match
    # the loaded model, we 400 — protects against silently using the wrong
    # model when a client expects a specific one.
    model: Optional[str] = None


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]
    model: str


@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(503, "model not loaded")
    return {"status": "ok", "model": _model.name}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    if _model is None:
        raise HTTPException(503, "model not loaded")

    if req.model is not None and req.model != _model.name:
        raise HTTPException(
            400,
            f"client requested model {req.model!r} "
            f"but service is serving {_model.name!r}",
        )

    embeddings = _model.encode_batch(req.texts)
    return EmbedResponse(embeddings=embeddings, model=_model.name)
