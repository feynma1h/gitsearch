"""Run the labelled query set against a running search service and
report per-query and aggregate metrics.

This is a measurement tool, not a test. Run it on demand when tuning
weights, swapping the embedding model, or changing the document
construction in the indexer. Output is human-readable and structured
enough to diff between runs.

Usage:
    # Defaults: hits localhost:8002, uses eval/queries.json, k=10.
    python -m eval.run

    # Compare configurations:
    python -m eval.run --weights similarity=1,stars=0.0,recency=0.0
    python -m eval.run --weights similarity=1,stars=0.5,recency=0.3

    # JSON output for machine consumption / CI diffing:
    python -m eval.run --json > runs/2026-05-03-baseline.json

Metrics:
    Recall@K — fraction of labelled relevant items that appear in top-K.
               (1, 1) means "we found them all."
    NDCG@K   — ranking-aware: gives more credit for relevant items
               appearing higher. Range [0, 1]; 1 is perfect ranking.

Both metrics treat relevance as binary (the labelled set lists repo
names that should be there; everything else is irrelevant).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_SERVICE_URL = "http://localhost:8002"
DEFAULT_QUERIES_PATH = Path(__file__).parent / "queries.json"
DEFAULT_K = 10


# ---------------------------------------------------------------------------
# Metrics — pure functions, easy to test in isolation if we ever want to.
# ---------------------------------------------------------------------------

def recall_at_k(retrieved: List[str], relevant: List[str], k: int) -> float:
    """Fraction of relevant items that appear in the top-k retrieved.

    If there are no relevant items for the query, recall is undefined;
    we return 1.0 (vacuously satisfied) so such queries don't drag the
    aggregate down. The harness shouldn't really contain such queries
    but be defensive.
    """
    if not relevant:
        return 1.0
    relevant_set = set(relevant)
    top_k = retrieved[:k]
    hits = sum(1 for r in top_k if r in relevant_set)
    return hits / len(relevant_set)


def ndcg_at_k(retrieved: List[str], relevant: List[str], k: int) -> float:
    """Normalised Discounted Cumulative Gain @ k for binary relevance.

    DCG = sum over i=1..k of rel_i / log2(i + 1)
    IDCG = DCG of the perfect ranking (all relevant items first).
    NDCG = DCG / IDCG, in [0, 1].
    """
    if not relevant:
        return 1.0
    relevant_set = set(relevant)
    top_k = retrieved[:k]

    dcg = sum(
        (1.0 / math.log2(i + 2))  # i is 0-indexed; rank starts at 1, so log2(i+1+1)
        for i, item in enumerate(top_k)
        if item in relevant_set
    )
    # Ideal: as many relevant items as possible packed at the top.
    ideal_count = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# Search client — plain stdlib so the eval has no extra deps.
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    full_name: str
    similarity: float
    hybrid_score: float


def call_search(
    base_url: str,
    query: str,
    limit: int,
    weights: Optional[Dict[str, float]],
) -> List[SearchResult]:
    """POST /search and return parsed hits."""
    payload: Dict = {"query": query, "limit": limit}
    if weights:
        payload["weights"] = weights

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"search request failed: {exc}") from exc

    return [
        SearchResult(
            full_name=h["full_name"],
            similarity=h["similarity"],
            hybrid_score=h["hybrid_score"],
        )
        for h in data["hits"]
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class QueryReport:
    query: str
    relevant: List[str]
    retrieved: List[str]
    recall: float
    ndcg: float


def evaluate(
    queries_path: Path,
    base_url: str,
    k: int,
    weights: Optional[Dict[str, float]],
) -> Tuple[List[QueryReport], Dict[str, float]]:
    with queries_path.open() as fh:
        data = json.load(fh)

    reports: List[QueryReport] = []
    for entry in data["queries"]:
        q = entry["query"]
        relevant = entry["relevant"]
        hits = call_search(base_url, q, k, weights)
        retrieved = [h.full_name for h in hits]
        reports.append(
            QueryReport(
                query=q,
                relevant=relevant,
                retrieved=retrieved,
                recall=recall_at_k(retrieved, relevant, k),
                ndcg=ndcg_at_k(retrieved, relevant, k),
            )
        )

    aggregate = {
        f"recall@{k}_mean":   statistics.fmean(r.recall for r in reports),
        f"recall@{k}_median": statistics.median(r.recall for r in reports),
        f"ndcg@{k}_mean":     statistics.fmean(r.ndcg for r in reports),
        f"ndcg@{k}_median":   statistics.median(r.ndcg for r in reports),
        "n_queries":          len(reports),
    }
    return reports, aggregate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_weights(spec: Optional[str]) -> Optional[Dict[str, float]]:
    """Parse 'similarity=1,stars=0.3,recency=0.2' into a dict."""
    if not spec:
        return None
    out: Dict[str, float] = {}
    for chunk in spec.split(","):
        if "=" not in chunk:
            raise SystemExit(f"bad --weights chunk: {chunk!r}")
        k, v = chunk.split("=", 1)
        out[k.strip()] = float(v.strip())
    return out


def _print_human(reports: List[QueryReport], aggregate: Dict[str, float]) -> None:
    print(f"{'recall':>8} {'ndcg':>8}  query")
    print("-" * 60)
    for r in sorted(reports, key=lambda x: x.ndcg):  # worst first — most informative
        print(f"{r.recall:>8.3f} {r.ndcg:>8.3f}  {r.query}")
        if r.recall < 1.0:
            missed = [name for name in r.relevant if name not in r.retrieved]
            if missed:
                print(f"{'':>17}  missed: {', '.join(missed[:3])}"
                      + ("…" if len(missed) > 3 else ""))
    print("-" * 60)
    for k, v in aggregate.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")


def main() -> None:
    p = argparse.ArgumentParser(description="Evaluate the search service.")
    p.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    p.add_argument("--queries", default=DEFAULT_QUERIES_PATH, type=Path)
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument(
        "--weights",
        help="Override weights, e.g. 'similarity=1,stars=0,recency=0'.",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = p.parse_args()

    try:
        reports, aggregate = evaluate(
            args.queries, args.service_url, args.k, _parse_weights(args.weights),
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json.dump(
            {
                "aggregate": aggregate,
                "reports": [r.__dict__ for r in reports],
                "config": {
                    "k": args.k,
                    "weights": _parse_weights(args.weights),
                    "service_url": args.service_url,
                },
            },
            sys.stdout,
            indent=2,
        )
        print()
    else:
        _print_human(reports, aggregate)


if __name__ == "__main__":
    main()
