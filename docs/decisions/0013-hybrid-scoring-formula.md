# ADR 0013: Hybrid scoring formula and over-fetch + re-rank

**Status:** superseded by ADR-0018 (the normalise-then-weighted-sum idea and
the component-contribution API contract carry forward; single-lane dense
retrieval and the log-stars term do not)
**Date:** 2026-05-03

## Context

Search needs to combine three signals into a single ranked list:

1. **Semantic similarity** to the user query (cosine similarity on
   embeddings produced by the model from ADR 0007).
2. **Popularity** (`stars`).
3. **Recency** (`pushed_at`).

A naive `similarity + log(stars) + recency_in_days` is wrong in
multiple ways:

- The three components live on incompatible scales. Cosine similarity is
  in `[0, 1]` for normalised embeddings of related text. `log10(stars)`
  for our corpus is in roughly `[2.3, 5.6]` (200 to ~400K stars). Raw
  "days since push" is unbounded. A direct sum is dominated by whichever
  component happens to have the largest absolute scale — in practice,
  log-stars — which means we've reinvented "sort by stars" with a small
  tie-break. That defeats the point of building a semantic search engine.
- Recency has no natural unit. "More recent is better" is true; "linearly
  more recent is linearly better" is not (a repo pushed yesterday vs.
  last week is barely distinguishable; one pushed last year vs. five
  years ago is hugely so).
- Even with normalised components, HNSW returns top-K *by similarity
  alone* — so if popularity has any weight, the eventual top-N by hybrid
  score may include items that weren't in the top-N by similarity, and
  we'll never see them.

## Decision

Three changes:

### a. Normalise each component to [0, 1] before combining.

```
sim_norm     = clamp(cosine_similarity, 0, 1)
stars_norm   = log10(1 + stars) / 6.0      (clamped <= 1)
recency_norm = 0.5 ** (days_since_pushed / half_life_days)
hybrid       = w_sim * sim_norm + w_stars * stars_norm + w_rec * recency_norm
```

- `LOG_STARS_DENOMINATOR = 6.0` is fixed (no per-query `MAX(stars)`
  lookup). Chosen so even GitHub's largest repo (~400K stars,
  log10 ≈ 5.6) stays under 1.0. A new megastar repo can't break the
  formula until star counts cross 10^6.
- Recency uses **half-life decay with a half-life of 365 days**: a
  repo pushed today scores 1.0, one year ago 0.5, two years ago 0.25.
  In SQL we express this as `EXP(-age_days * LN(2) / half_life_days)`,
  which is mathematically identical and keeps everything in
  `double precision`. Half-life is the natural knob for tuning ("a
  year ago scores half") and is request-overridable.
- `pushed_at IS NULL` repos score 0 on recency. Rare but defensible:
  treat "never pushed" as maximally stale.

### b. Default weights: similarity dominates.

```
w_sim = 1.0    w_stars = 0.3    w_recency = 0.2
```

The semantic signal is what makes this product different from sorting
GitHub by stars. Stars and recency exist as tie-breakers and as
demoters of stale-but-similar matches. There is a ranking unit test
(`test_hybrid_score_similarity_dominates_with_default_weights`) that
pins this down: an 0.85-similarity 200-star repo must beat an
0.40-similarity 400K-star repo. If a future weight tweak breaks that
property, the test fails.

Weights are per-request overridable (`weights.similarity`, etc.) so
quality experiments don't require redeploys.

### c. Over-fetch from HNSW, then re-rank.

The search SQL is structured as a CTE: top-K-by-similarity inside the
CTE (where `K = max(50, 5 * limit)`, capped at 500), then ORDER BY the
hybrid score in the outer SELECT and LIMIT to the requested N.

Without over-fetching, items in positions K+1..N by similarity but
high enough on stars/recency to win on hybrid score would be
invisible. With it, the re-ranker has enough candidates to surface
them.

### d. Compute the hybrid score in SQL, not Python.

Two reasonable places: pull top-K candidates and re-rank in Python, or
do everything in one SQL query. We do it in SQL:

- One round trip vs. two (top-K SELECT, then full-row SELECT after
  re-rank).
- Postgres can ORDER BY the computed score and LIMIT in one pass
  without materialising the full candidate set.
- The same WHERE filters need to apply to the candidate query *and*
  affect what's available for re-ranking; doing it in one statement
  removes the chance of those drifting.

The Python implementation in `ranking.py` exists for testability and as
the canonical statement of what the formula *means*. It and the SQL in
`db.py` must stay in sync — a manual discipline, called out in both
files' docstrings.

## Alternatives considered

- **Skip normalisation, tune weights to compensate.** Could in principle
  produce the same ranking, but the weights become uninterpretable
  ("0.04? is that big or small?"). Normalisation makes a weight of 0.3
  mean "this component contributes up to 0.3 of the final score."
- **Linear recency decay over a fixed window.** Simpler to explain, but
  has a discontinuity at the window edge and treats "recent" too coarsely.
  Exponential decay is the standard for this shape and has one knob.
- **Query-time `MAX(stars)` for the stars denominator.** Self-adapts as
  the corpus grows. Tempting but rejected: the score for a given repo
  would then depend on whatever the most-starred repo in the corpus
  has, so a new megastar entering would silently demote every other
  repo even though nothing about them changed. That makes scores hard
  to reason about, hard to cache, and hard to compare across eval
  runs. Stability of rankings is more valuable than stability of the
  exact `[0, 1]` range — and we already clamp with `LEAST(1.0, ...)`
  in SQL, so a denominator that's slightly low merely flattens the
  very top of the curve. It doesn't break ordering. Adding the query
  also costs one round trip per request or a cache that needs
  invalidation. Net negative.
- **Percentile-based denominator (e.g., `log10(1 + p99_stars)`).**
  Splits the difference between a fixed value and `MAX`: adapts to
  corpus *shape* changes, ignores single outliers. Worth reaching for
  if the fixed denominator ever does become genuinely wrong (see
  "What would change this decision"); not worth the complexity now.
- **Reciprocal Rank Fusion (RRF).** Combine ranks (not scores) from a
  similarity sort and a stars sort. Robust to scale problems and used
  in Elastic-style hybrid search. Real candidate, but RRF's strength is
  combining heterogeneous *ranking systems* (BM25 + dense vectors), not
  combining a score with a metadata signal. Also doesn't naturally
  accommodate recency. Worth revisiting if we add a BM25 lane.
- **Per-component min-max normalisation across the candidate set.**
  More adaptive (every query gets its own scale), but turns the score
  into something that depends on the candidate set, so two near-identical
  queries can produce wildly different scores for the same repo. Bad for
  caching and bad for debugging.
- **No over-fetch (use HNSW's top-N directly).** Saves work but visibly
  underapplies the hybrid weights. Wrong default.

## Consequences

- ✅ Each weight is interpretable. `w_stars = 0.3` means stars can shift
  the score by at most 0.3.
- ✅ Per-request overrides make it cheap to A/B (`?weights.stars=0` for
  pure semantic, `?weights.recency=0.5` for fresher results, etc.).
- ✅ Over-fetch + re-rank gives the hybrid weights real effect with no
  operational complexity beyond a slightly bigger LIMIT inside the CTE.
- ✅ Single SQL statement keeps latency tight and removes a class of
  "the candidate query and the rerank disagreed about filters" bugs.
- ⚠️ The formula is now a piece of policy that lives in two files
  (`ranking.py` and `db.py`). Worth duplicating for the testability
  benefit, but a refactor that changes one and forgets the other will
  silently corrupt rankings. Both files' docstrings call this out.
- ⚠️ Filters interact with HNSW: very selective `WHERE` clauses can
  return fewer rows than expected because pgvector applies them during
  graph traversal. Mitigated by the over-fetch and `hnsw.ef_search = 100`.
  pgvector 0.8's iterative scans are the proper fix when this becomes
  a real problem.
- ⚠️ Without an evaluation harness, weight tuning is vibes-driven. A
  small labelled query set (e.g., 30 `(query, expected_repos)` pairs
  scored with NDCG@10) is the recommended next step.

## What would change this decision

- Real measured search quality is bad and the weights aren't the lever.
  Likely culprit is then the source document construction (see indexer's
  `document_builder.py`), not this ADR.
- The most-starred repo in the corpus crosses ~10^6 stars (so
  `log10(1+stars) > 6` and the stars term blows past 1.0 even after
  clamping affects more than the absolute top of the leaderboard).
  Bump `LOG_STARS_DENOMINATOR` to 7, or switch to a percentile-based
  denominator. As of 2026, GitHub's biggest repo is ~400K stars, so
  this trigger has a wide margin.
- We add a BM25 / lexical lane. RRF over (lexical rank, semantic rank)
  becomes the better combiner; the metadata signals (stars, recency)
  could either fold into RRF as a third rank source or stay as a
  separate weighted boost on top.
- Stars distribution shifts dramatically (e.g., GitHub introduces a new
  signal that obsoletes star count). Renegotiate the popularity term.
- Latency budget tightens enough that the over-fetch becomes a problem.
  Lower `DEFAULT_OVERFETCH_MULTIPLIER` and accept the recall loss, or
  upgrade to pgvector iterative scans.
