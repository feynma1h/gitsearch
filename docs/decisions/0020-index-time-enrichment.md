# ADR 0020: Index-time enrichment — mined curation, versioned embeddings, and a dark criticality signal

**Status:** accepted
**Date:** 2026-08-06

## Context

ADR 0018 shipped three-lane hybrid retrieval with one documented gate
miss: "machine learning framework python" surfaced only tensorflow of
the required pytorch/tensorflow/scikit-learn trio. The mechanism was
fully understood — neither missing repo carries the token "framework"
in any indexed field, and no fusion parameter can surface what
retrieval never produced. The designed fix was index-time document
enrichment: manufacture the category vocabulary users type but
canonical repos' metadata lacks.

The research synthesis ranked LLM enrichment (docTTTTTquery lineage)
as the highest-leverage single change, with deps.dev dependent counts
as the criticality signal stars can't fake. This phase adds a third,
cheaper source it surfaced only implicitly: the corpus itself holds
~2.7K awesome-list repos — thousands of hours of human curation, each
entry a repo link with a hand-written description under a category
heading ("Frameworks", "Vector Database"). That is anchor-text-analog
data, the signal web search has always leaned on, available for the
cost of parsing markdown.

Three user-mandated reversibility constraints shaped everything:
enrichment lives in its own table and one migration removes it;
enriched embeddings sit under a versioned label beside the originals
and serving flips by config; absent enrichment degrades to exactly
phase-1 behaviour with no LLM anywhere in the serving path.

## Decision

### a. Enrichment is data with provenance, in its own table

`repository_enrichment` (migration 0009) is keyed
`(repo_id, source ∈ {'awesome-mined','llm'})` with text payload
columns (description, queries[], aliases[], categories[]), provenance
(`model`, `prompt_version`, `generated_at`), and its own generated
tsvector (aliases weight A, categories+queries B, description C —
mirroring 0007's "how a person names the thing" weighting). The
0007/0008 functions on `repositories` were NOT altered: a generated
column's expression is row-local by definition, so "the tsvector
references the enrichment" is achieved in the serving SQL's join, not
in the column — same reversibility guarantee (`DROP TABLE` + config
flag), no table rewrite in either direction, and no lying to Postgres
about immutability. `repository_signals` (migration 0010) gets the
same containment for deps.dev data.

### b. Awesome-list mining before any LLM

`crawler/src/mine_awesome.py` refetches the full README of every
awesome-tagged repo (stored copies are capped at 8KB, which beheads a
catalog file; only mined entries are stored, the corpus cap stands),
parses link entries with heading-trail tracking, and aggregates per
target repo: descriptions preferring phrasings that differ from the
repo's own GitHub description, aliases behind a two-vote quorum once a
repo appears in ≥4 lists (one curator's idiosyncratic anchor text must
not become a name-weight match), categories by cross-list frequency.
The 2026-08-06 pass: 2,656 lists fetched, 208K entries parsed, 56,156
corpus repos enriched (~21% of the corpus, strongly head-weighted).
pytorch's row carries "Frameworks" from awesome-deep-learning's
section heading — the literal missing token.

### c. Retrieval: enrichment joins the FTS lane without changing its shape

Phase 1's measured-fast plan (coverage evaluated inline during one
bitmap heap scan, ordered by coverage-then-stars) survives. A CTE
folds enrichment rows sharing ≥1 query lexeme into per-repo boolean
term flags (`bool_or`, hash-aggregated — cheaper than concatenating
tsvectors); the main scan hash-LEFT-JOINs it. A `UNION ALL` arm
admits repos reachable only through enrichment (the Doc2Query case:
matching synthetic queries when the repo's own fields match nothing).
Measured on the broadest gate query: ~120ms server-side for the lane
(vs ~30ms unenriched; scales down with enrichment matches on narrower
queries). Membership through enrichment demands the same pairwise-AND
bar as the light fields, probed per row — cross-source single-term
pairs don't confer membership (accepted; coverage still combines
cross-source).

**Enrichment coverage is capped at completing one term:**
`coverage = own_terms + LEAST(1, enrichment_only_terms)`. The first
eval of the uncapped design measured why: topical lists paint every
repo they link with their heading-trail vocabulary ("Game Engine
Development" lands on bootstrap via one list's tooling section), and
with stars ordering inside tiers, megastars holding two stray
enrichment terms took over category queries wholesale ("game engine"
top-10 became bootstrap/vue/d3/electron; nDCG −0.031 while canary
recall rose +0.131). Legitimate wins have the opposite shape — the
repo's own curated fields cover most of the query and enrichment
supplies the one word the metadata lacks (pytorch:
machine+learning+python own, "framework" mined; scikit-learn
likewise). Frequency-filtering categories instead would have killed
the same tail categories the gate win rides on ("Frameworks for
Training" is exactly as rare on scikit-learn as "Game Engine
Development" is on bootstrap — the difference is own-field support,
which the cap encodes). Verified live post-cap: the noise evicted,
the trio intact, and awesome-python dropped out of the gate query's
top tier (its machine/learning coverage was itself enrichment-derived
— the cap polices hubs too).

Two adjacent fixes ride along:

- **Punctuation-normalised exact-name matching** closes ADR 0018's
  name-squatter landmine: `translate(lower(name), '-._', '')` equality
  (expression indexes in 0009) pins vercel/next.js into the exact tier
  for "nextjs", where score-then-stars ordering beats the repo
  literally named nextjs. Verified live.
- **A criticality term** `w_crit × sat(dependent_count)` joins the
  blend and the API contract (`criticality_contribution`), with
  `w_crit = 0.0` — dark. deps.dev's public per-version dependent
  counts probed ecosystem-inconsistent (PyPI defaults sane; NPM
  fragmented ~15×; Cargo all zeros), so the ingest stores a
  max-over-sampled-versions lower bound and the weight moves only if
  the eval proves it helps. Upgrade path if promoted: OpenSSF
  criticality_score's aggregated dataset.

`PHASE2_RETRIEVAL=off` serves the phase-1 statement verbatim (same
parameter list, same response columns, criticality computed from a
constant NULL). That flag is the DB-side rollback lever (drop the
tables, flip the flag) and made the code deploy verifiable as
no-behavior-change before enrichment switched on: local flag-off
matched live production exactly on probe queries before cutover.

### d. Embeddings: versioned labels, copied where identical

Enriched documents (aliases/categories/queries/description folded in
between metadata and README head, inside bge-small's ~512-token
window) embed under label `BAAI/bge-small-en-v1.5+enrich-v1` beside
the originals — `repository_embeddings` is keyed (repo_id, model_name)
for exactly this (ADR 0006). Unenriched repos build byte-identical
documents under either label, so their vectors are copied SQL-side
(`make copy-embeddings-label`), never recomputed; only enriched repos
re-embed. The label names a document construction, not a new encoder —
query vectors stay bge-small and stay in-space.

The halfvec HNSW index became per-label and partial
(`WHERE model_name = '<label>'`), with the label a literal in the
dense lane's SQL (a bound parameter can't match a partial-index
predicate at plan time). Sequencing matters and is recorded in the
Makefile: build the base label's partial index, deploy the
literal-label service, drop the label-blind index, THEN bulk-load the
new label — so no insert ever pays per-row HNSW graph insertion
against another label's index. Serving flips labels with the
`EMBEDDINGS_MODEL_LABEL` env var; rollback is the same var pointed
back.

### e. LLM generation: full corpus on Gemini, with a second-family control

`indexer/pipeline/enrich_llm.py` implements the Doc2Query-- design:
batch APIs (50% discount) + structured outputs produce 5-8 synthetic
queries, an ≤80-word plain-vocabulary description, aliases, and
category tags per repo; generated queries are kept only if they embed
within cosine ≥ 0.45 of their source document (the consistency filter
that made Doc2Query-- beat unfiltered expansion). Submission requires
an explicit `--i-approve-the-cost` flag after a printed estimate.

Provider economics decided the run (user-approved 2026-08-06): Gemini
flash-lite batch versus a multiple-of-cost premium on Haiku 4.5 whose
only purchase is keeping the generator outside the eval judge's model
family. The Gemini path was approved **with the family conflict
handled as a measured control rather than a caveat**: after the LLM
variant's eval, a ~300-pair sample of the pairs where enriched
documents won is re-judged by a second-family judge (Haiku), and
divergence between the judges marks the graded numbers as soft. The
canary suite — hand-curated, judge-free — remains the primary anchor
either way, per the eval README's own drift rule. A 100-repo
validation batch preceded the full submit: 100/100 parsed, zero
consistency-filter drops, average 6.7 queries per repo.

**Cost correction (2026-08-07, recorded because the paper trail
should show real numbers):** the run was initially estimated at ~$24
using the retired 2.5-flash-lite tier's batch rates ($0.05/$0.20 per
MTok) against the 3.1-flash-lite model actually being run — whose
true batch rates are $0.125/$0.75. Final reconciled full-corpus
cost, summed from per-request usage metadata across all 33 batch
response files: **$66.45** (227.6M tokens in / 50.7M out over
244,396 requests — per-repo ~931 in / 207 out, thinking pinned to
zero and verified zero in usage metadata; all requests succeeded,
244,388 rows collected, 8 dropped client-side as unparseable). The
user re-approved completion at the corrected figure mid-run. Two quota mechanics also
shaped the run and are worth a future reader's minute: Tier-1
flash-lite batch allows only 10M enqueued tokens (jobs above ~9K
requests are unlaunchable — the drain runs sequential 8K chunks),
and prepaid-credit depletion doesn't fail queued batch jobs, it
silently parks them until topped up.

## Alternatives considered

- **Altering 0007/0008's generated columns to include enrichment.**
  Rejected: generated expressions can't reference other tables;
  faking it via a falsely-IMMUTABLE function would go silently stale
  on enrichment writes, and adding/removing the column rewrites the
  ~900MB table both ways.
- **Aggregating enrichment tsvectors at query time**
  (`tsvector_agg`). Built, measured (~257ms server-side, nested-loop
  join shape), replaced by bool_or term flags in the phase-1 scan
  shape (~120ms). The aggregate remains in migration 0009 for
  debugging sessions.
- **LLM-first enrichment.** The plan's ordering — mine human curation
  first, measure, then decide LLM spend — held: mining alone closed
  the recorded gate miss and costs nothing per month.
- **Feeding mined aliases into the exact-name pin.** Rejected for
  this phase: anchor texts are one curator's word; the exact-name tier
  is popularity-independent and thus squatter-sensitive. Aliases index
  at weight A (matching) only. The normalised-name fix covers the
  documented landmine from the repo's own name, no third-party trust
  required.
- **A single label-blind halfvec index over both labels.** Rejected:
  doubles the serving graph, halves effective recall per ef_search
  visit for near-duplicate twin vectors, and bulk-loading the second
  label through it costs hours of per-row graph insertion on Micro
  compute.

## Consequences

- ✅ The ADR 0018 gate miss closes structurally (verified live:
  pytorch/tensorflow/scikit-learn all top-10 for the gate phrasing,
  via mined category vocabulary alone).
- ✅ Every enrichment artifact is separable: one `DROP TABLE` per
  source-family, one env var per serving behaviour, versioned rows
  for every generated text.
- ✅ 56K repos (head-weighted) carry human-curated category text at
  zero LLM cost; the LLM path exists with cost gates when coverage
  beyond the head is wanted.
- ⚠️ FTS lane cost on the broadest category queries roughly triples
  (~30→~120ms server-side; warm E2E stays well under the 1.5s bar).
  Watch p95 as enrichment grows; the bool_or CTE scales with
  enrichment rows matching any query lexeme.
- ⚠️ Awesome-list *hub* repos rank high for category queries they
  curate (they legitimately match the vocabulary). The eval judges
  whether that helps or hurts; if it hurts, a topics-based demotion
  of `awesome`-tagged repos in category contexts is the recorded
  first lever.
- ⚠️ The criticality column ships dark. If the eval promotes it, the
  frontend's "why this rank" bar needs a fourth segment (the API
  already sums it into `hybrid_score`).
- ⚠️ Mined text is third-party content in the index (weights A-C).
  The quorum/shape filters bound alias abuse; a malicious list
  climbing into the corpus could still seed descriptions. Bounded by
  the 200-star corpus floor and per-repo caps; revisit if abuse
  appears.

## Results

Judged 2026-08-06 (same UMBRELA judge and append-only qrels as ADR
0018, pool complete at every point — judged@10 = 1.000). Run files in
`search/eval/history/`; `python -m eval.compare` reproduces every row.
Deltas are against the phase-1 gated baseline re-scored on the grown
pool (0.820–0.822 depending on comparison pool).

| variant | ΔnDCG@10 | Δcanary recall@10 | trio gate |
|---|---|---|---|
| mined FTS only, rrf_k=20 | −0.017 (p<0.001) | +0.093 | pass |
| mined FTS only, rrf_k=50 | +0.010 | +0.028 | pass |
| + enriched embeddings, rrf_k=20 | −0.011 (p=0.01) | **+0.157** | pass |
| + enriched embeddings, rrf_k=50 | **+0.018** (p<0.0001) | +0.091 | pass |

Readings, recorded plainly:

- **The designed fix works.** The gate query surfaces
  pytorch + tensorflow + scikit-learn in the top 10 at every point;
  under enriched embeddings scikit-learn reaches #2 on semantic
  match, not just lexical coverage.
- **Enriched embeddings dominate FTS-only at fixed k** (better on
  both axes) — the docTTTTTquery-lineage claim held for mined text.
- **rrf_k is a genuine precision↔canon-recall dial**: sharp fusion
  (k=20) maximises canary recall; flatter fusion (k=50) lets the
  dense lane counterbalance enrichment-boosted FTS tiers and flips
  nDCG positive. Both endpoints remain per-request tunable.
- **The pre-declared ship gate (ΔnDCG ≥ +0.03) was not met** at any
  swept point; best is +0.018. The canary — the drift-immune,
  hand-curated denominator this suite exists for — improves at every
  point, by up to +0.157 (30% relative). The residual nDCG losses
  concentrate where mined category vocabulary pulls popular adjacent
  repos above niche-precise ones ("self-hosted X" queries); the
  coverage cap halved that effect but did not eliminate it.
  Per-repo generated vocabulary (the LLM variant, unrun) is the
  designed precision fix and remains gated on its own measured
  marginal value.
- **The criticality term did not earn its weight**: w_crit = 0.2 on
  the enriched configuration measured −0.005 nDCG / +0.010 canary
  against the same configuration without it — noise-level, slightly
  precision-negative, consistent with the version-sampled lower
  bound's ecosystem skew. The default stays 0.0; the recorded upgrade
  path (OpenSSF criticality_score's aggregated dataset) is the next
  candidate if the signal is wanted.

## What would change this decision

- The eval showing mined-FTS-only regressing nDCG or canary recall →
  flip `PHASE2_RETRIEVAL=off` (config), investigate, keep the data.
- The LLM variant measuring ≥ +0.03 nDCG over mining alone at its
  quoted cost → approve the batch and re-gate; measuring less →
  record and stop at mined enrichment.
- deps.dev criticality failing its sweep → the weight stays 0.0 and
  the ADR's upgrade path (criticality_score dataset) becomes the next
  candidate, or the term is removed at the next contract revision.
- Enrichment volume (full-corpus LLM) pushing the FTS lane past the
  latency bar → scope the bool_or CTE probe or split enrichment's GIN
  by source.
