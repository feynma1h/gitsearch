"""Run a query set against a running search service and report metrics.

This is a measurement tool, not a test. Two modes share one command:

  Legacy (default): eval/queries.json entries carry hand-labelled
  `relevant` lists -> binary Recall@K / NDCG@K, exactly as before.
  `make eval` still lands here.

  v2: point --queries at eval/queries_v2.json (no labels inline) and
  metrics come from the judged pool (--qrels eval/qrels.json, graded
  nDCG / recall / judged@K) and the hand-curated canary suite
  (--canary eval/canary.json). Save the raw ranked lists with
  --save-run for pooling (eval/judge.py) and A/B tests (eval/compare.py).

Usage:
    # Legacy regression check against a local service:
    python -m eval.run

    # Capture a run of production for pooling + comparison:
    python -m eval.run --service-url https://…run.app \
        --queries eval/queries_v2.json --save-run eval/runs/baseline.json \
        --system baseline-dense-prod --sleep 2.1

    # Score a saved configuration once qrels exist:
    python -m eval.run --queries eval/queries_v2.json \
        --qrels eval/qrels.json --canary eval/canary.json

    # Compare configurations by overriding weights (legacy mode):
    python -m eval.run --weights similarity=1,stars=0.0,recency=0.0

Metrics (see eval/metrics.py for definitions): binary Recall@K / NDCG@K
in legacy mode; graded nDCG@K, Recall@K (grade>=2), Judged@K, and canary
recall@K in v2 mode. The service is scale-to-zero in production, so the
client tolerates one cold start (~60-65 s, ADR 0019) via a generous
timeout + retry.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .metrics import (
    canary_recall_at_k,
    judged_at_k,
    mean,
    ndcg_at_k as graded_ndcg_at_k,
    recall_at_k_graded,
)

DEFAULT_SERVICE_URL = "http://localhost:8002"
DEFAULT_QUERIES_PATH = Path(__file__).parent / "queries.json"
DEFAULT_K = 10
# Store more than we score: the judging pool wants top-20 (see judge.py)
# even while metrics are @10.
DEFAULT_FETCH_K = 20
# Rides out one production cold start (~60-65 s measured, ADR 0019) and
# matches the server's own 90 s request cap.
REQUEST_TIMEOUT = 90.0
REQUEST_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Legacy binary metrics — unchanged; queries.json regression numbers must
# stay comparable across the v2 work.
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
    ideal_count = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# Search client — plain stdlib so the eval has no extra deps.
# ---------------------------------------------------------------------------

def call_search(
    base_url: str,
    query: str,
    limit: int,
    weights: Optional[Dict[str, float]],
    timeout: float = REQUEST_TIMEOUT,
) -> List[str]:
    """POST /search and return the ranked full_names.

    Retries a couple of times with backoff: the first request of a
    session may hit the embedding service's cold start, and production
    rate-limits bursts with 429s.
    """
    payload: Dict = {"query": query, "limit": limit}
    if weights:
        payload["weights"] = weights

    last_error: Optional[Exception] = None
    for attempt in range(REQUEST_ATTEMPTS):
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return [h["full_name"] for h in data["hits"]]
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:          # rate limited: wait out the window
                time.sleep(20.0 * (attempt + 1))
                continue
            if exc.code >= 500 and attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(5.0)          # embedding cold start settling
                continue
            break
        except OSError as exc:
            # URLError, raw socket TimeoutError (read timeouts surface
            # unwrapped), connection resets — all retryable.
            last_error = exc
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(5.0)
                continue
    raise RuntimeError(f"search request failed for {query!r}: {last_error}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class QueryReport:
    query: str
    stratum: Optional[str]
    retrieved: List[str]
    metrics: Dict[str, float] = field(default_factory=dict)
    relevant: List[str] = field(default_factory=list)   # legacy labels, if any


def evaluate(
    queries_path: Path,
    base_url: str,
    k: int,
    fetch_k: int,
    weights: Optional[Dict[str, float]],
    qrels: Optional[dict],
    canary_by_query: Dict[str, List[str]],
    sleep: float,
    timeout: float,
    concurrency: int = 1,
) -> Tuple[List[QueryReport], Dict[str, float]]:
    with queries_path.open() as fh:
        data = json.load(fh)

    entries = data["queries"]
    if concurrency > 1:
        # Against a local/private instance only — production rate-limits.
        with concurrent.futures.ThreadPoolExecutor(concurrency) as pool:
            retrieved_lists = list(pool.map(
                lambda e: call_search(
                    base_url, e["query"], max(fetch_k, k), weights,
                    timeout=timeout,
                ),
                entries,
            ))
    else:
        retrieved_lists = []
        for i, entry in enumerate(entries):
            retrieved_lists.append(call_search(
                base_url, entry["query"], max(fetch_k, k), weights,
                timeout=timeout,
            ))
            if sleep and i < len(entries) - 1:
                time.sleep(sleep)

    reports: List[QueryReport] = []
    for entry, retrieved in zip(entries, retrieved_lists):
        q = entry["query"]
        report = QueryReport(
            query=q,
            stratum=entry.get("stratum"),
            retrieved=retrieved,
            relevant=entry.get("relevant", []),
        )

        if report.relevant:
            report.metrics["recall"] = recall_at_k(retrieved, report.relevant, k)
            report.metrics["ndcg"] = ndcg_at_k(retrieved, report.relevant, k)
        if qrels is not None:
            grades = {
                name: j["grade"]
                for name, j in qrels["judgments"].get(q, {}).items()
            }
            report.metrics["ndcg_graded"] = graded_ndcg_at_k(retrieved, grades, k)
            report.metrics["judged"] = judged_at_k(retrieved, grades, k)
            rec = recall_at_k_graded(retrieved, grades, k)
            if rec is not None:
                report.metrics["recall_grade2"] = rec
        if q in canary_by_query:
            report.metrics["canary_recall"] = canary_recall_at_k(
                retrieved, canary_by_query[q], k
            )

        reports.append(report)

    aggregate: Dict[str, float] = {"n_queries": len(reports)}
    legacy = [r for r in reports if "recall" in r.metrics]
    if legacy:
        aggregate[f"recall@{k}_mean"] = mean([r.metrics["recall"] for r in legacy])
        aggregate[f"recall@{k}_median"] = statistics.median(
            r.metrics["recall"] for r in legacy
        )
        aggregate[f"ndcg@{k}_mean"] = mean([r.metrics["ndcg"] for r in legacy])
        aggregate[f"ndcg@{k}_median"] = statistics.median(
            r.metrics["ndcg"] for r in legacy
        )
    for key, label in [
        ("ndcg_graded", f"ndcg_graded@{k}_mean"),
        ("recall_grade2", f"recall@{k}_grade2_mean"),
        ("judged", f"judged@{k}_mean"),
        ("canary_recall", f"canary_recall@{k}_mean"),
    ]:
        vals = [r.metrics[key] for r in reports if key in r.metrics]
        if vals:
            aggregate[label] = mean(vals)
            aggregate[label.replace("_mean", "_n")] = len(vals)
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
    # Sort worst-first by whichever ndcg flavour is present — when tuning,
    # you want to see the queries you're failing on, not the easy wins.
    def sort_key(r: QueryReport) -> float:
        return r.metrics.get("ndcg_graded", r.metrics.get("ndcg", 0.0))

    has_legacy = any("recall" in r.metrics for r in reports)
    has_graded = any("ndcg_graded" in r.metrics for r in reports)
    if has_legacy:
        print(f"{'recall':>8} {'ndcg':>8}  query")
        print("-" * 60)
        for r in sorted(reports, key=sort_key):
            if "recall" not in r.metrics:
                continue
            print(f"{r.metrics['recall']:>8.3f} {r.metrics['ndcg']:>8.3f}  {r.query}")
            missed = [name for name in r.relevant if name not in r.retrieved]
            if missed:
                print(f"{'':>17}  missed: {', '.join(missed[:3])}"
                      + ("…" if len(missed) > 3 else ""))
        print("-" * 60)
    if has_graded:
        print(f"{'g-ndcg':>8} {'judged':>8} {'canary':>8}  query")
        print("-" * 72)
        for r in sorted(reports, key=sort_key):
            if "ndcg_graded" not in r.metrics:
                continue
            canary = r.metrics.get("canary_recall")
            print(f"{r.metrics['ndcg_graded']:>8.3f} "
                  f"{r.metrics['judged']:>8.3f} "
                  f"{canary if canary is not None else float('nan'):>8.3f}  "
                  f"{r.query}")
        print("-" * 72)
    for k, v in aggregate.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")


def main() -> None:
    p = argparse.ArgumentParser(description="Evaluate the search service.")
    p.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    p.add_argument("--queries", default=DEFAULT_QUERIES_PATH, type=Path)
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument("--fetch-k", type=int, default=DEFAULT_FETCH_K,
                   help="Results to request/store per query (>= --k).")
    p.add_argument(
        "--weights",
        help="Override weights, e.g. 'similarity=1,stars=0,recency=0'.",
    )
    p.add_argument("--qrels", type=Path,
                   help="Graded judgments (eval/qrels.json) for v2 metrics.")
    p.add_argument("--canary", type=Path,
                   help="Canary suite (eval/canary.json) for canary recall.")
    p.add_argument("--save-run", type=Path,
                   help="Write the ranked lists to this run file for "
                        "pooling/comparison.")
    p.add_argument("--system", default="unnamed",
                   help="System label stored in the run file.")
    p.add_argument("--sleep", type=float, default=0.0,
                   help="Seconds between requests (use ~2.1 against "
                        "production; it rate-limits at 30/minute).")
    p.add_argument("--concurrency", type=int, default=1,
                   help="Parallel requests. Only against a local "
                        "instance with SEARCH_RATE_LIMIT raised; "
                        "overrides --sleep.")
    p.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT)
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = p.parse_args()

    qrels = None
    if args.qrels:
        with args.qrels.open() as fh:
            qrels = json.load(fh)
    canary_by_query: Dict[str, List[str]] = {}
    if args.canary:
        with args.canary.open() as fh:
            canary_by_query = {
                e["query"]: e["expected"] for e in json.load(fh)["queries"]
            }

    try:
        reports, aggregate = evaluate(
            args.queries, args.service_url, args.k, args.fetch_k,
            _parse_weights(args.weights), qrels, canary_by_query,
            args.sleep, args.timeout, args.concurrency,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.save_run:
        run = {
            "system": args.system,
            "date": _dt.date.today().isoformat(),
            "service_url": args.service_url,
            "queries_file": str(args.queries),
            "fetch_k": args.fetch_k,
            "weights": _parse_weights(args.weights),
            "results": {r.query: r.retrieved for r in reports},
        }
        args.save_run.parent.mkdir(parents=True, exist_ok=True)
        with args.save_run.open("w") as fh:
            json.dump(run, fh, indent=1)
            fh.write("\n")
        print(f"run saved: {args.save_run}", file=sys.stderr)

    if args.json:
        json.dump(
            {
                "aggregate": aggregate,
                "reports": [r.__dict__ for r in reports],
                "config": {
                    "k": args.k,
                    "weights": _parse_weights(args.weights),
                    "service_url": args.service_url,
                    "queries": str(args.queries),
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
