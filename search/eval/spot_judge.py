"""Second-family spot-judge: Haiku re-grades a sample of Gemini's calls.

The qrels were judged by gemini-3.1-flash-lite, and phase 2's enrichment
text was *written* by the same model family — a same-family judge could
in principle prefer vocabulary its sibling generated. This re-judges a
seeded sample of the candidate run's top-10 pairs with Anthropic's
claude-haiku-4-5 under the identical UMBRELA prompt and reports
agreement. High agreement = the gains aren't a family artifact.

Usage:
    python -m eval.spot_judge --run eval/runs/<candidate>.json \
        [--sample 300] [--out eval/runs/spot-judge-haiku.json]
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .judge import (
    build_passage,
    fetch_docs,
    judge_pair,
    load_qrels,
)

QRELS_PATH = Path(__file__).parent / "qrels.json"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--sample", type=int, default=300)
    p.add_argument("--seed", type=int, default=20260807)
    p.add_argument("--out", default="eval/runs/spot-judge-haiku.json")
    args = p.parse_args()

    run = json.loads(Path(args.run).read_text())
    judgments = load_qrels(QRELS_PATH)["judgments"]

    pool = []
    for q, ranked in run["results"].items():
        for repo in ranked[:10]:
            g = judgments.get(q, {}).get(repo, {}).get("grade")
            if g is not None:
                pool.append((q, repo, g))
    rng = random.Random(args.seed)
    sample = rng.sample(pool, min(args.sample, len(pool)))
    print(f"pool={len(pool)} judged pairs in top-10; sampling {len(sample)}")

    docs = fetch_docs(sorted({repo for _, repo, _ in sample}))
    rows, missing = [], 0
    for i, (q, repo, gem) in enumerate(sample, 1):
        doc = docs.get(repo)
        if doc is None:
            missing += 1
            continue
        grade = judge_pair("anthropic", "claude-haiku-4-5", q,
                           build_passage(doc))
        if grade is None:
            missing += 1
            continue
        rows.append({"query": q, "repo": repo, "gemini": gem,
                     "haiku": grade})
        if i % 50 == 0:
            print(f"  {i}/{len(sample)} re-judged")

    n = len(rows)
    exact = sum(1 for r in rows if r["gemini"] == r["haiku"]) / n
    g_rel = [r["gemini"] >= 2 for r in rows]
    h_rel = [r["haiku"] >= 2 for r in rows]
    bin_agree = sum(1 for a, b in zip(g_rel, h_rel) if a == b) / n
    # Cohen's kappa on the binarised grades.
    p_yes_g = sum(g_rel) / n
    p_yes_h = sum(h_rel) / n
    p_e = p_yes_g * p_yes_h + (1 - p_yes_g) * (1 - p_yes_h)
    kappa = (bin_agree - p_e) / (1 - p_e) if p_e < 1 else float("nan")
    bias = sum(r["gemini"] - r["haiku"] for r in rows) / n

    summary = {
        "n": n, "skipped": missing,
        "exact_agreement": round(exact, 3),
        "binary_agreement_rel2": round(bin_agree, 3),
        "kappa_rel2": round(kappa, 3),
        "mean_grade_bias_gemini_minus_haiku": round(bias, 3),
    }
    print(json.dumps(summary, indent=2))
    Path(args.out).write_text(json.dumps(
        {"summary": summary, "rows": rows}, indent=1))
    print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()
