"""Tests for the embedding client's validation logic.

We don't mock out the HTTP call here — that's an integration test. We do
test ``_validate``, which is pure and catches the most common failure
modes (wrong batch size, wrong dimension).
"""

from __future__ import annotations

import pytest

from pipeline.client import EmbeddingClient, EmbeddingServiceError
from pipeline.config import EMBEDDING_DIM


def test_validate_passes_with_correct_shape():
    embeddings = [[0.0] * EMBEDDING_DIM for _ in range(3)]
    EmbeddingClient._validate(embeddings, expected_count=3)  # no raise


def test_validate_raises_on_wrong_count():
    embeddings = [[0.0] * EMBEDDING_DIM for _ in range(2)]
    with pytest.raises(EmbeddingServiceError, match="Expected 3"):
        EmbeddingClient._validate(embeddings, expected_count=3)


def test_validate_raises_on_wrong_dimension():
    embeddings = [[0.0] * (EMBEDDING_DIM - 1)]  # one too short
    with pytest.raises(EmbeddingServiceError, match="dim"):
        EmbeddingClient._validate(embeddings, expected_count=1)


def test_validate_raises_on_zero_dim():
    embeddings = [[]]  # empty vector
    with pytest.raises(EmbeddingServiceError, match="dim 0"):
        EmbeddingClient._validate(embeddings, expected_count=1)


def test_validate_passes_on_empty_batch():
    """An empty batch is a degenerate but valid case."""
    EmbeddingClient._validate([], expected_count=0)
