"""Compare two run files on the judged qrels + canary suite, with a
paired significance test and the phase-1 ship gate.

Usage:
    python -m eval.compare --baseline runs/baseline.json \
        --candidate runs/hybrid.json [--qrels eval/qrels.json] \
        [--canary eval/canary.json] [--k 10] [--gate]

The gate (from the search-v2 phase-1 spec) passes only if ALL hold:
  1. mean ΔnDCG@10 (candidate - baseline) >= +0.03,
  2. Fisher randomization p < 0.05 on the per-query nDCG deltas,
  3. mean canary recall@10 strictly improves,
  4. the release-gate query "machine learning framework python" has
     pytorch/pytorch, tensorflow/tensorflow AND scikit-learn/scikit-learn
     in the candidate's top-10.
With --gate the exit code is 0 on pass / 2 on fail, so it can guard a
deploy script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

from .metrics import (
    canary_recall_at_k,
    fisher_randomization_p,
    judged_at_k,
    mean,
    ndcg_at_k,
    recall_at_k_graded,
)

DEFAULT_QRELS = Path(__file__).parent / "qrels.json"
DEFAULT_CANARY = Path(__file__).parent / "canary.json"

GATE_QUERY = "machine learning framework python"
GATE_REPOS = ["pytorch/pytorch", "tensorflow/tensorflow", "scikit-learn/scikit-learn"]
GATE_MIN_DELTA = 0.03
GATE_ALPHA = 0.05


def load_run(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def grades_for(qrels: dict, query: str) -> Dict[str, int]:
    return {
        name: j["grade"] for name, j in qrels["judgments"].get(query, {}).items()
    }


def main() -> None:
    p = argparse.ArgumentParser(description="A/B compare two eval runs.")
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--qrels", type=Path, default=DEFAULT_QRELS)
    p.add_argument("--canary", type=Path, default=DEFAULT_CANARY)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--gate", action="store_true",
                   help="Exit 2 unless the phase-1 ship gate passes.")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    run_a, run_b = load_run(args.baseline), load_run(args.candidate)
    with args.qrels.open() as fh:
        qrels = json.load(fh)
    with args.canary.open() as fh:
        canary = json.load(fh)

    queries = sorted(set(run_a["results"]) & set(run_b["results"]))
    dropped = (set(run_a["results"]) | set(run_b["results"])) - set(queries)
    if dropped:
        print(f"note: {len(dropped)} queries present in only one run — skipped",
              file=sys.stderr)

    k = args.k
    ndcg_a: List[float] = []
    ndcg_b: List[float] = []
    judged_a: List[float] = []
    judged_b: List[float] = []
    rec_a: List[float] = []
    rec_b: List[float] = []
    per_query: List[dict] = []

    for q in queries:
        g = grades_for(qrels, q)
        na = ndcg_at_k(run_a["results"][q], g, k)
        nb = ndcg_at_k(run_b["results"][q], g, k)
        ndcg_a.append(na)
        ndcg_b.append(nb)
        judged_a.append(judged_at_k(run_a["results"][q], g, k))
        judged_b.append(judged_at_k(run_b["results"][q], g, k))
        ra = recall_at_k_graded(run_a["results"][q], g, k)
        rb = recall_at_k_graded(run_b["results"][q], g, k)
        if ra is not None and rb is not None:
            rec_a.append(ra)
            rec_b.append(rb)
        per_query.append({"query": q, "ndcg_a": na, "ndcg_b": nb, "delta": nb - na})

    deltas = [row["delta"] for row in per_query]
    delta_mean = mean(deltas)
    p_value = fisher_randomization_p(deltas)

    can_a: List[float] = []
    can_b: List[float] = []
    for entry in canary["queries"]:
        q = entry["query"]
        if q in run_a["results"] and q in run_b["results"]:
            can_a.append(canary_recall_at_k(run_a["results"][q], entry["expected"], k))
            can_b.append(canary_recall_at_k(run_b["results"][q], entry["expected"], k))

    gate_top = run_b["results"].get(GATE_QUERY, [])[:k]
    gate_hits = [r for r in GATE_REPOS if r in gate_top]
    checks = {
        f"ndcg_delta>={GATE_MIN_DELTA}": delta_mean >= GATE_MIN_DELTA,
        f"p<{GATE_ALPHA}": p_value < GATE_ALPHA,
        "canary_recall_up": mean(can_b) > mean(can_a),
        "gate_query_top10": len(gate_hits) == len(GATE_REPOS),
    }
    gate_pass = all(checks.values())

    summary = {
        "n_queries": len(queries),
        f"ndcg@{k}": {"baseline": mean(ndcg_a), "candidate": mean(ndcg_b),
                      "delta": delta_mean, "fisher_p": p_value},
        f"recall@{k}_grade2": {"baseline": mean(rec_a), "candidate": mean(rec_b),
                               "n": len(rec_a)},
        f"canary_recall@{k}": {"baseline": mean(can_a), "candidate": mean(can_b),
                               "n": len(can_a)},
        f"judged@{k}": {"baseline": mean(judged_a), "candidate": mean(judged_b)},
        "gate_query_hits": gate_hits,
        "gate_checks": checks,
        "gate_pass": gate_pass,
    }

    if args.json:
        json.dump({"summary": summary, "per_query": per_query}, sys.stdout, indent=2)
        print()
    else:
        a_name = run_a.get("system", "baseline")
        b_name = run_b.get("system", "candidate")
        print(f"A: {a_name}   B: {b_name}   ({len(queries)} shared queries)\n")
        print(f"{'metric':>22} {'A':>8} {'B':>8} {'delta':>8}")
        rows = [
            (f"ndcg@{k}", mean(ndcg_a), mean(ndcg_b)),
            (f"recall@{k} (rel>=2)", mean(rec_a), mean(rec_b)),
            (f"canary recall@{k}", mean(can_a), mean(can_b)),
            (f"judged@{k}", mean(judged_a), mean(judged_b)),
        ]
        for label, a, b in rows:
            print(f"{label:>22} {a:>8.3f} {b:>8.3f} {b - a:>+8.3f}")
        print(f"\nFisher randomization p = {p_value:.4f} "
              f"(ndcg deltas, {len(deltas)} pairs)")
        worst = sorted(per_query, key=lambda r: r["delta"])[:5]
        best = sorted(per_query, key=lambda r: -r["delta"])[:5]
        print("\nbiggest wins:")
        for row in best:
            print(f"  {row['delta']:+.3f}  {row['query']}")
        print("biggest losses:")
        for row in worst:
            print(f"  {row['delta']:+.3f}  {row['query']}")
        if min(mean(judged_a), mean(judged_b)) < 0.90:
            print("\nALARM: judged@10 below 0.90 — pool the unjudged docs "
                  "and re-run the judge before trusting these numbers.")
        print(f"\ngate query top-{k} hits: {gate_hits}")
        print("gate checks: " + ", ".join(
            f"{name}={'PASS' if ok else 'FAIL'}" for name, ok in checks.items()
        ))
        print(f"GATE: {'PASS' if gate_pass else 'FAIL'}")

    if args.gate and not gate_pass:
        sys.exit(2)


if __name__ == "__main__":
    main()
