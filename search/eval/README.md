# Evaluation harness

Measures search quality well enough to make ship/no-ship decisions.
Two tiers share one runner:

- **Quick regression** (`make eval`): the original 5 hand-labelled
  queries in `queries.json`, binary Recall@10 / NDCG@10. Seconds to
  run; catches gross regressions.
- **Decision-grade** (`queries_v2.json` + friends): 200 stratified
  queries, LLM-judged pooled qrels, a hand-curated canary suite, and a
  paired significance test. This is what phase gates run on
  (ADR 0018 was the first).

Everything here is stdlib-only Python — no extra deps to run an eval.
(`requirements-dev.txt` installs ir_measures purely so the test suite
can verify our metric math against trec_eval's.)

## The pieces

| File | What it is |
| --- | --- |
| `queries.json` | 5 legacy seeds with inline `relevant` labels. Unchanged, still the `make eval` default. |
| `queries_v2.json` | 200 queries, stratified: 50 navigational (incl. typos), 75 category, 75 task. No labels inline. |
| `canary.json` | 50 category queries with hand-picked canonical answers, every repo verified present in the corpus. Judge-independent ground truth; frozen (append-only) after the 2026-08-05 hand-review pass. |
| `qrels.json` | Graded (0–3) relevance judgments, pooled from every compared system's top-20, produced by `judge.py`. Append-only. |
| `run.py` | Runs a query set against a service; saves ranked lists as a *run file*; computes whichever metrics its inputs allow. |
| `history/` | Frozen run files that a shipped decision was gated on, plus the spot-judge agreement report (a `{summary, rows}` file, not a run file). `runs/` itself is gitignored scratch. |
| `judge.py` | UMBRELA LLM judge (Gemini by default) over pooled run files → `qrels.json`. |
| `spot_judge.py` | Second-family re-grade: `claude-haiku-4-5` re-judges a seeded sample of a run's top-10 pairs under the same UMBRELA prompt, reporting exact/binary agreement, Cohen's kappa and mean grade bias. Tests the primary judge for same-family bias — see ADR 0020. |
| `compare.py` | A/B between two run files: ΔnDCG@10 with a Fisher randomization p-value, recall, canary recall, judged@10, and the ship gate. |
| `metrics.py` | The metric definitions (graded nDCG, recall@k grade≥2, judged@k, canary recall, significance test). |

## Decision-grade workflow

```bash
# 0. Env: DATABASE_URL (for the judge's doc fetch) and GEMINI_API_KEY
#    (or OPENAI_API_KEY / ANTHROPIC_API_KEY) — put both in the repo .env.

# 1. Capture a run per system you want to compare (top-20 stored for
#    pooling, metrics reported @10). Against production, pace the
#    requests — it rate-limits at 30/minute:
python -m eval.run --service-url https://<prod>.run.app \
    --queries eval/queries_v2.json --sleep 2.1 \
    --save-run eval/runs/$(date +%F)-baseline.json --system baseline

#    Against a local instance (start it with SEARCH_RATE_LIMIT raised),
#    sweeps go wide open:
python -m eval.run --queries eval/queries_v2.json --concurrency 6 \
    --weights rrf_k=60 \
    --save-run eval/runs/$(date +%F)-k60.json --system hybrid-k60

# 2. Judge the pool (incremental — only unjudged pairs cost anything;
#    --dry-run first shows the count and est. cost):
python -m eval.judge --runs 'eval/runs/*.json' --dry-run
python -m eval.judge --runs 'eval/runs/*.json'

# 3. Compare + gate:
python -m eval.compare --baseline eval/runs/...-baseline.json \
    --candidate eval/runs/...-k60.json --gate
```

`--gate` encodes the ADR 0018 ship rule: ΔnDCG@10 ≥ +0.03 at p < 0.05,
canary recall up, and the release-gate query ("machine learning
framework python") surfacing pytorch + tensorflow + scikit-learn in the
top 10. Exit code 2 on failure, so it can guard a deploy.

## Rules that keep the numbers honest

- **Pool before judging.** qrels must contain the union of top-20 from
  *every* system being compared, or the newer system's unjudged docs
  score as irrelevant. `judge.py` handles this; the `Judged@10` column
  in `compare.py` is the alarm (investigate anything under 0.90).
- **Append-only labels.** Never edit or delete a judgment or a canary
  entry to make a run look better. Re-judging means a version bump and
  a full re-judge.
- **The judge never sees popularity.** Stars are excluded from the
  passage the judge grades — relevance is the judge's job, ranking
  popularity is the engine's.
- **Different model family.** The judge (Gemini) is deliberately not
  from the family the product pipeline uses (Claude, for guides). If
  you must fall back to `--provider anthropic`, the caveat belongs in
  whatever you write up, plus a `--spot-check` pass.
- **Canary is the drift anchor.** Judge models change; the hand-picked
  canary labels don't. If judged metrics and canary recall disagree
  about a change's direction, trust the canary and investigate.

## Metrics

- **nDCG@10** (graded, linear gains, trec_eval-compatible) — primary.
- **Recall@10 (grade ≥ 2)** — of the docs the judge called
  substantively relevant, how many the system surfaced.
- **Canary recall@10** — of the hand-picked canonical repos, how many
  showed up. Immune to judge drift by construction.
- **Judged@10** — pool-coverage alarm, not a quality metric.
- **Fisher randomization p** — two-sided, on paired per-query nDCG
  deltas; the standard IR significance test.

The default human-readable output sorts worst queries first — when
tuning, you want to see what you're failing, not the easy wins.
