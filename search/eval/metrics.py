"""Graded-relevance metrics and the paired significance test for the v2
eval. Pure functions, stdlib only — same policy as the rest of eval/:
no third-party deps, so the harness runs anywhere Python does.

The math follows trec_eval conventions so numbers are comparable to the
IR literature:

  nDCG@k    — linear gains (gain = grade), log2 discount:
              DCG = sum grade_i / log2(rank_i + 1), rank starting at 1.
              Ideal ranking drawn from ALL judged docs for the query,
              not just retrieved ones. Unjudged docs count as grade 0.
  Recall@k (grade >= min) — fraction of docs judged >= min_grade that
              appear in the top-k. Undefined (None) when the query has
              no docs at that grade; callers skip those queries.
  Judged@k  — fraction of the top-k that has any judgment at all. The
              pool-bias alarm: if a new system surfaces many unjudged
              docs, its nDCG is unfairly deflated until they're judged
              (alarm threshold: < 0.90).
  Canary recall@k — fraction of a hand-curated expected list present in
              the top-k. Judge-independent by construction.

Significance: Fisher randomization (permutation) test on paired
per-query deltas — the standard for IR A/B comparison (used by ranx as
its default `fisher` mode). Deterministic via a fixed seed.

`search/tests/test_eval_metrics.py` pins these functions down, including
a parity check against ir_measures when that package is installed (it is
in requirements-dev; the harness itself never imports it).
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Sequence


def ndcg_at_k(retrieved: Sequence[str], grades: Dict[str, int], k: int) -> float:
    """Graded nDCG@k with linear gains. ``grades`` maps doc id -> 0..3;
    docs absent from ``grades`` are unjudged and gain 0."""
    if k <= 0:
        return 0.0
    dcg = sum(
        grades.get(doc, 0) / math.log2(i + 2)
        for i, doc in enumerate(retrieved[:k])
    )
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k_graded(
    retrieved: Sequence[str], grades: Dict[str, int], k: int, min_grade: int = 2
) -> Optional[float]:
    """Fraction of docs judged >= min_grade found in the top-k. None when
    the query has no such docs (metric undefined, not vacuously 1)."""
    relevant = {doc for doc, g in grades.items() if g >= min_grade}
    if not relevant:
        return None
    found = sum(1 for doc in retrieved[:k] if doc in relevant)
    return found / len(relevant)


def judged_at_k(retrieved: Sequence[str], grades: Dict[str, int], k: int) -> float:
    """Fraction of the top-k that has a judgment. Empty top-k counts as
    fully judged (nothing is missing a label)."""
    top = retrieved[:k]
    if not top:
        return 1.0
    return sum(1 for doc in top if doc in grades) / len(top)


def canary_recall_at_k(
    retrieved: Sequence[str], expected: Sequence[str], k: int
) -> float:
    """Fraction of the hand-picked expected list present in the top-k."""
    if not expected:
        return 1.0
    top = set(retrieved[:k])
    return sum(1 for doc in expected if doc in top) / len(expected)


def fisher_randomization_p(
    deltas: Sequence[float], n_permutations: int = 100_000, seed: int = 0
) -> float:
    """Two-sided paired Fisher randomization test.

    ``deltas`` are per-query metric differences (system B - system A).
    Under H0 the sign of each delta is arbitrary, so we flip signs at
    random and count how often the permuted |mean| reaches the observed
    |mean|. The +1/+1 correction keeps p > 0 (Davison & Hinkley).
    """
    if not deltas:
        return 1.0
    observed = abs(sum(deltas) / len(deltas))
    if observed == 0.0:
        return 1.0
    rng = random.Random(seed)
    n = len(deltas)
    hits = 0
    for _ in range(n_permutations):
        s = sum(d if rng.random() < 0.5 else -d for d in deltas)
        if abs(s / n) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n_permutations + 1)


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean; 0.0 for an empty sequence (callers report n too)."""
    return sum(values) / len(values) if values else 0.0
