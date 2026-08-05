"""Pin down the eval metric implementations (eval/metrics.py).

These are the numbers phase gates are decided on, so the math gets the
same treatment as the ranking formula: hand-computed examples plus a
parity check against ir_measures (the trec_eval wrapper) when it is
installed. The harness itself never imports ir_measures — stdlib only.
"""

from __future__ import annotations

import math
import random

import pytest

from eval.metrics import (
    canary_recall_at_k,
    fisher_randomization_p,
    judged_at_k,
    mean,
    ndcg_at_k,
    recall_at_k_graded,
)


# ---------------------------------------------------------------------------
# nDCG (graded, linear gains)
# ---------------------------------------------------------------------------

def test_ndcg_hand_computed() -> None:
    grades = {"a": 3, "b": 0, "c": 2, "d": 1}
    retrieved = ["a", "b", "c"]
    # DCG = 3/log2(2) + 0/log2(3) + 2/log2(4) = 3 + 0 + 1 = 4
    # IDCG (ideal = [3, 2, 1]) = 3/1 + 2/log2(3) + 1/2
    idcg = 3 + 2 / math.log2(3) + 0.5
    assert ndcg_at_k(retrieved, grades, 3) == pytest.approx(4 / idcg)


def test_ndcg_perfect_ranking_is_one() -> None:
    grades = {"a": 3, "b": 2, "c": 1}
    assert ndcg_at_k(["a", "b", "c"], grades, 10) == pytest.approx(1.0)


def test_ndcg_unjudged_docs_gain_nothing() -> None:
    grades = {"a": 2}
    # A pile of unjudged docs above the judged one costs rank credit.
    assert ndcg_at_k(["x", "y", "a"], grades, 3) < 1.0
    assert ndcg_at_k(["a"], grades, 3) == pytest.approx(1.0)


def test_ndcg_no_judgments_is_zero() -> None:
    assert ndcg_at_k(["a", "b"], {}, 10) == 0.0


def test_ndcg_ideal_uses_all_judged_not_just_retrieved() -> None:
    # The best judged doc was never retrieved; nDCG must be < 1.
    grades = {"missing": 3, "a": 1}
    assert ndcg_at_k(["a"], grades, 10) == pytest.approx(
        1 / (3 + 1 / math.log2(3))
    )


# ---------------------------------------------------------------------------
# Recall (grade >= 2), Judged, canary recall
# ---------------------------------------------------------------------------

def test_recall_graded() -> None:
    grades = {"a": 3, "b": 1, "c": 2, "d": 0}
    assert recall_at_k_graded(["a", "b"], grades, 2) == pytest.approx(0.5)
    assert recall_at_k_graded(["a", "c"], grades, 2) == pytest.approx(1.0)


def test_recall_graded_undefined_when_nothing_relevant() -> None:
    assert recall_at_k_graded(["a"], {"a": 1}, 10) is None


def test_judged_at_k() -> None:
    grades = {"a": 0, "b": 3}
    assert judged_at_k(["a", "b", "x"], grades, 3) == pytest.approx(2 / 3)
    assert judged_at_k([], grades, 10) == 1.0


def test_canary_recall() -> None:
    assert canary_recall_at_k(["a", "b", "c"], ["a", "d"], 3) == pytest.approx(0.5)
    assert canary_recall_at_k(["a", "b"], [], 10) == 1.0


# ---------------------------------------------------------------------------
# Fisher randomization
# ---------------------------------------------------------------------------

def test_fisher_consistent_improvement_is_significant() -> None:
    deltas = [0.1] * 30
    assert fisher_randomization_p(deltas, n_permutations=20_000) < 0.001


def test_fisher_noise_is_not_significant() -> None:
    deltas = [0.1, -0.1] * 15
    assert fisher_randomization_p(deltas, n_permutations=20_000) > 0.5


def test_fisher_empty_and_zero() -> None:
    assert fisher_randomization_p([]) == 1.0
    assert fisher_randomization_p([0.0, 0.0]) == 1.0


def test_fisher_deterministic() -> None:
    deltas = [0.05, 0.2, -0.1, 0.3, 0.0, 0.12]
    a = fisher_randomization_p(deltas, n_permutations=5_000)
    b = fisher_randomization_p(deltas, n_permutations=5_000)
    assert a == b


def test_mean() -> None:
    assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert mean([]) == 0.0


# ---------------------------------------------------------------------------
# Parity with ir_measures / trec_eval, when available
# ---------------------------------------------------------------------------

def test_ndcg_parity_with_ir_measures() -> None:
    ir_measures = pytest.importorskip("ir_measures")

    rng = random.Random(7)
    qrels = {}
    run = {}
    for qi in range(25):
        qid = f"q{qi}"
        docs = [f"d{di}" for di in range(rng.randint(3, 30))]
        qrels[qid] = {d: rng.randint(0, 3) for d in rng.sample(docs, len(docs) // 2)}
        ranked = rng.sample(docs, len(docs))
        run[qid] = {d: float(len(ranked) - i) for i, d in enumerate(ranked)}

    # Constructed via the operator API — ir_measures' string parser
    # (parse_measure) still uses ast.Num, which Python 3.14 removed.
    measure = ir_measures.nDCG @ 10
    theirs = ir_measures.calc_aggregate([measure], qrels, run)[measure]

    ours = mean([
        ndcg_at_k(
            sorted(run[qid], key=run[qid].__getitem__, reverse=True),
            qrels[qid],
            10,
        )
        for qid in qrels
    ])
    assert ours == pytest.approx(theirs, abs=1e-8)
