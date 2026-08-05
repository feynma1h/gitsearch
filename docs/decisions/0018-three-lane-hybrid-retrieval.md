# ADR 0018: Three-lane hybrid retrieval with RRF fusion and an additive popularity blend

**Status:** accepted (supersedes ADR 0013's scoring formula; retrieval architecture is new)
**Date:** 2026-08-05

## Context

The engine retrieved candidates through a single dense-vector lane
(cosine KNN over `bge-small-en-v1.5` embeddings) and re-ranked them with
the ADR 0013 weighted sum. On the 20K-repo corpus that looked fine; on
the full 267K corpus it visibly failed the queries a search engine for
repositories most needs to win:

- **Category queries.** "machine learning framework python" did not
  surface pytorch, tensorflow, or scikit-learn — not in the top 10, not
  in the top 500 by similarity. Their embeddings describe what the
  projects *are* ("tensors and dynamic neural networks…"), not the
  category vocabulary a person types, and the cosine neighborhood of a
  generic phrase is crowded with thousands of small repos whose entire
  README is that phrase. The stars weight never sees the canonical
  repos because candidate generation already lost them.
- **Name queries.** Exact-name lookups ("fastapi", "redis") depended on
  the embedding happening to rank the named repo first among hundreds
  of similarly-named or similarly-described projects. Typos
  ("pytorhc") had no path to the right answer at all.

This is not a tuning problem. Single-vector retrieval provably cannot
serve entity-heavy top-k at this scale (DeepMind's LIMIT bound;
EntityQuestions' dense-vs-BM25 gap), and every production package
search that works — GitHub's own, npms.io, libraries.io, crates.io —
pairs text relevance with an authority prior. The full evidence review
lives outside the repo (research synthesis, 2026-08-05, ~30 sources);
this ADR records what we changed and why.

Two structural constraints shaped the design:

- Stay on Supabase Postgres + Cloud Run (ADR 0012); no second search
  system, no managed BM25 flight risks (ParadeDB is unavailable on
  Supabase; Neon dropped it).
- The public API's score-component contract (ADR 0013's three
  contributions summing to `hybrid_score`) powers the frontend's
  tune-ranking sliders and "why this rank?" bar and must keep working.

## Decision

### a. Candidate generation: three lanes in one SQL statement

All three lanes run inside a single statement (`search/service/db.py`)
and see identical WHERE filters:

1. **Full-text** — two generated tsvector columns (migrations
   0007/0008): a README-inclusive one for *matching* (weight `D` =
   first 2,500 README chars, so full websearch AND matches reach
   phrase-in-README docs) and a README-free "light" one (name tokens /
   topics + language / description) for *membership and scoring*.
   Lane membership = any-two-terms in the light fields (pairwise-AND
   expansion; single-term for one-word queries) OR a full websearch
   match; lane order = **(term coverage, stars)** — how many distinct
   query terms the repo's topics + primary language cover, then
   popularity within each coverage tier. Top 200. Two designs died on
   measurement before this one: cover-density ranking (`ts_rank_cd`,
   any weighting) puts the canonical repos at ranks 267–1804 for
   "machine learning framework python" because low-star repos carry
   the query as their literal name, and README-inclusive membership at
   OR breadth visits ~66K heap tuples (~3 s/query on Micro compute).
   Coverage-tiering fixed the ranks (pytorch 1804 → 52); the light
   column fixed the cost (the whole statement: 38 ms server-side).
   Restricting the coverage *count* to topics/language rather than all
   light fields measurably changed almost nothing further (1 of 200
   queries) — kept anyway as cheap insurance against
   description-boilerplate tier inflation. Coverage-tiering is
   GitHub's own ordering insight applied per tier; topics carry
   exactly the category vocabulary ("machine-learning",
   "vector-database") that canonical repos' descriptions lack, and the
   corpus probe showed topics made every eval-category canonical repo
   lexically matchable.
2. **Dense** — the existing embedding KNN, now through a `halfvec`
   expression HNSW index (`halfvec_ip_ops`; the embeddings are
   L2-normalised, so inner product ≡ cosine, at half the index size).
   pgvector 0.8 iterative scans (`relaxed_order`) keep filtered
   searches from returning short lanes. Top 200.
3. **Name** — pg_trgm on `name`: exact (`lower(full_name)/lower(name)`
   equality), prefix (`LIKE 'q%'` on an expression btree), and fuzzy
   (`%` similarity against the trigram GIN, threshold 0.3; enabled
   only for queries of ≤ 2 words — typos are short, and
   trigram-scanning sentences costs seconds for zero recall). Top 50.
   This is the lane that catches "pytorhc".

Lanes fuse by **weighted Reciprocal Rank Fusion**:
`rrf = Σ_lane w_lane / (k + rank_lane)` with defaults
`w_fts = w_dense = 1.0`, `w_name = 0.5`, `k = 50` (swept 20/50/60 in
the eval; per-request overridable for future sweeps). RRF combines
heterogeneous rankings without comparable scores — exactly the reason
ADR 0013 deferred it until a lexical lane existed.

### b. Ranking: additive blend over normalised components

```
final = demotion × ( w_rel  × minmax(rrf over candidate set)
                   + w_pop  × sat(stars)
                   + w_rec  × recency )
sat(stars) = stars / (stars + pivot)
pivot      = clamp(geomean(candidate stars), 100, 20 000)
recency    = floor + (1 − floor) × 0.5^(days_since_push / half_life),  floor = 0.25
demotion   = 0.5 archived, 0.8 fork, else 1.0
```

- **Additive and saturated, never multiplicative.** The npms-style
  `relevance × popularity^13.5` design distorts scores and rewards
  star-farming; saturation means 100K stars ≈ 10K stars near the cap,
  so popularity is a bounded boost that cannot drown relevance. The
  pivot adapts per query (the candidate set's geometric mean), so
  "typical for this query" scores 0.5.
- **Popularity never gates recall.** Candidate generation is purely
  relevance-based; stars only reorder what relevance already found.
- **Recency floors instead of decaying to zero** — a finished,
  canonical library must not sink out of sight for being stable.
- **Exact-name-first (crates.io's rule, verbatim).** A candidate whose
  `name` or `full_name` equals the query sorts above everything,
  popularity-independent. Typing a thing's name returns that thing.
- Default weights stay `w_rel = 1.0, w_pop = 0.3, w_rec = 0.2` — the
  sliders' semantics carry over unchanged.

### c. The API contract survives with one reinterpretation

`similarity_contribution` now carries the *fused relevance*
contribution (`w_rel × minmax(rrf)`) rather than weighted cosine; the
raw cosine stays in `similarity` for display (0.0 for the ~22K repos
with no embedding — newly reachable through the lexical lanes). The
three contributions still sum to `hybrid_score` (demotion scales each
component, preserving the invariant), so the stacked bar and sliders
work untouched. A new `exact_name` boolean rides along. One knock-on:
scores are now comparable within a response, not across queries —
min-max normalisation is per-candidate-set, the trade ADR 0013
explicitly declined and this ADR explicitly accepts (the eval-gated
quality win outweighs cross-query score stability, which nothing
user-facing relied on).

### d. Measurement: the phase-0 eval foundation

Decisions of this size are no longer eyeballed on 5 seed queries. The
eval stack this ADR was gated on (`search/eval/`):

- 200 stratified queries (`queries_v2.json`): 50 navigational
  (including typos and stale-owner forms), 75 category, 75 task.
- A 50-query **canary suite** (`canary.json`) of category → canonical
  repos, human-curated (user veto pass 2026-08-05), every entry
  verified present in the corpus — a judge-drift-immune true-recall
  denominator.
- **UMBRELA judge** (`judge.py`): the TREC-adopted prompt, 0–3 graded,
  temperature 0, one (query, repo) pair per call, Gemini 2.5
  Flash-Lite — deliberately a different model family from the product
  pipeline (guides use Claude Haiku). Judgments are pooled from the
  top-20 of every compared system and append-only (`qrels.json`).
- Metrics (`metrics.py`, stdlib, parity-tested against ir_measures):
  nDCG@10 (graded, primary), Recall@10 (grade ≥ 2), canary recall@10,
  Judged@10 (pool-bias alarm at < 0.90), and paired Fisher
  randomization for significance (`compare.py`).

Ship gate, decided before any results were seen: **ΔnDCG@10 ≥ +0.03 at
p < 0.05, canary recall up, and "machine learning framework python"
surfacing pytorch + tensorflow + scikit-learn in the top 10.**

### e. Result (the numbers this shipped on)

Judged 2026-08-06: 200 queries, 7,476 pooled (query, repo) pairs graded
0–3 by `gemini-3.1-flash-lite` (UMBRELA, temperature 0; the originally
planned 2.5-flash-lite had been closed to new API projects). Candidate
= this ADR's design at its shipped defaults (rrf_k = 20) vs. the
dense-only production baseline, both captured on the same corpus:

| metric | baseline | hybrid | Δ |
|---|---|---|---|
| nDCG@10 (graded) | 0.731 | 0.824 | **+0.093** (gate ≥ +0.03; Fisher p < 0.0001) |
| Recall@10 (grade ≥ 2) | 0.305 | 0.365 | +0.060 |
| Canary recall@10 | 0.398 | 0.527 | **+0.129** (gate: improve) |
| Judged@10 | 1.000 | 1.000 | pool complete — no unjudged-doc bias |

Three of the four gate checks pass, two of them by wide margins. The
fourth — the literal query "machine learning framework python"
surfacing pytorch AND tensorflow AND scikit-learn in the top 10 — does
not (tensorflow reaches #2; the other two never enter the candidate
pool for that phrasing). The mechanism is fully understood: neither
repo carries the token "framework" in any indexed field, and neither
gets dense-lane support for the phrase among 244K embeddings, so no
setting of the formula's parameters (rrf_k 20–200, lane weights,
popularity weight 0.3–0.7 — all swept) can surface what retrieval
never produced. Manufacturing exactly this missing category vocabulary
is phase 2's LLM enrichment, by design. Shipped 2026-08-06 with this
condition recorded as the known limitation to be re-gated after
phase 2.

Sweep note: rrf_k swept 20/50/60/200 gave canary recall 0.527 / 0.484
/ 0.469 / 0.415 — sharp fusion wins monotonically, so 20 is the
shipped default.

Reproduction: the query set, canary suite, and graded qrels live in
`search/eval/`; the frozen baseline and candidate run files in
`search/eval/history/`; `python -m eval.compare` recomputes this table.

## Alternatives considered

- **Rewrite on a dedicated search stack** (Elastic/Meilisearch/
  Typesense/Vespa/Qdrant, managed or self-hosted). Rejected: $25–350/mo
  or a new stateful system to operate; every viable alternative reuses
  ~90% of what exists anyway. Postgres already holds the corpus, and
  Supabase's own documented hybrid pattern is exactly this design.
- **A true BM25 sidecar** (bm25s / SQLite-FTS5) for proper IDF.
  Deferred, not rejected: `ts_rank_cd`'s missing IDF is second-order on
  our short, uniform documents. The sidecar remains the escape hatch if
  eval says lexical scoring is the bottleneck.
- **Multiplicative popularity** (npms/libraries.io style). Rejected on
  the record: score distortion, gameability, and the pattern's flagship
  died. Saturated-additive is the web-search-lineage design.
- **Popularity as a fourth RRF lane** (rank by stars, fuse). Rejected:
  RRF would hand stars an implicit, uncontrollable weight; the blend
  keeps popularity's influence explicit, bounded, and slider-tunable.
- **Query-intent routing / LLM query rewriting / HyDE.** Deferred /
  deferred / rejected (HyDE measures flat-to-negative on modern
  embedders). The three-lane fusion handles most routing implicitly —
  a name-shaped query simply wins its lane.
- **Keeping fp32 HNSW as the dense lane.** halfvec halves the index
  (~477 MB → ~half) at parity recall (verified before the fp32 index
  was dropped); on a 16 GB disk that headroom matters.

## Consequences

- ✅ Category and name queries stop being structural failures; the
  22.6K repos without embeddings become searchable.
- ✅ Ranking policy is explainable per result: fused relevance +
  saturated stars + floored recency, components exposed as before.
- ✅ Weights, lane weights, and `rrf_k` are per-request tunable —
  future eval sweeps need no redeploys.
- ✅ The eval harness turns future retrieval claims into measurements
  (this ADR's own gate is reproducible from committed run files).
- ⚠️ The ranking formula lives in two places (`ranking.py` and the SQL
  in `db.py`) — same sync discipline as before, now with more terms.
- ⚠️ Per-query min-max relevance normalisation means scores aren't
  comparable across queries (accepted trade, see above).
- ⚠️ Coverage ties are decided by stars inside the FTS lane, so a
  popular partially-matching repo can outrank an obscure
  better-matching one there. That is the intended trade for category
  queries; the dense lane and the AND-match arm remain
  popularity-blind paths into the pool if it ever measures badly.
- ⚠️ `search_tsv` + `search_tsv_light` are generated columns: every
  future crawler refresh pays the tsvector computation on write, and
  the pair must stay consistent with any future doc-construction
  change (phase-2 enrichment will touch both). Acceptable at our
  refresh volumes; a trigger-free design was the explicit goal.

## What would change this decision

- Eval shows lexical scoring (not retrieval) is the residual
  bottleneck → add the bm25s/FTS5 sidecar (afternoon-sized, no new
  stateful service).
- Phase-2 enrichment (LLM doc2query + deps.dev dependents) lands →
  re-run the same gate; the blend gains a criticality term
  (`sat(dependents)`) per the research plan.
- p95 warm latency exceeds ~1.5 s at the 267K scale → revisit lane
  depths and the OR tier before anything architectural.
- The corpus outgrows Postgres FTS comfort (~millions of rows) or the
  cold-start story must die entirely → the Cloudflare serving-layer
  option (Workers + D1 FTS5 + Vectorize) is the recorded phase-4 path.
