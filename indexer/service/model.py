"""Wraps the sentence-transformers model.

Loaded once at process startup. ``encode_batch`` is the only entry point;
the FastAPI handler calls it.
"""

from __future__ import annotations

import logging
import threading
from typing import List

logger = logging.getLogger(__name__)


class EmbeddingModel:
    """Lazy-loaded sentence-transformer model.

    Loading the model takes ~5 seconds and ~500MB of RAM. We do it once at
    startup and serve all subsequent requests against the in-memory model.

    A single :class:`threading.Lock` serialises calls into ``encode_batch``
    because sentence-transformers' encode method is not safe to call from
    multiple threads on the same model. FastAPI under uvicorn runs request
    handlers in a thread pool, so this matters.
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # lazy import

        logger.info("Loading model %s ...", model_name)
        self._model = SentenceTransformer(model_name)
        self._model_name = model_name
        self._lock = threading.Lock()
        logger.info("Model %s loaded.", model_name)

    @property
    def name(self) -> str:
        return self._model_name

    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """Encode a batch of texts to embedding vectors.

        Normalises to unit length so cosine distance == 1 - dot product,
        which simplifies similarity computation downstream and matches
        what the bge model card recommends.
        """
        with self._lock:
            vectors = self._model.encode(
                texts,
                batch_size=len(texts),
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        return vectors.tolist()
