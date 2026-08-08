"""Database access for the search service.

A single entry point: given a query (text + vector) + filters + weights,
return the top-N repos ranked by the hybrid blend.

Retrieval is three lanes in one SQL statement (ADR 0018; enrichment
arms added by ADR 0020 behind config.PHASE2_RETRIEVAL):

  1. Full-text — matches any content lexeme in the README-free light
     fields (``search_tsv_light``: name, topics, language, description)
     or the full websearch query anywhere including the README
     (``search_tsv``), or — phase 2 — the repo's enrichment terms
     (``repository_enrichment_terms``, the folded lexeme union of its
     mined/generated aliases, categories, descriptions and synthetic
     queries; sql/0011). Ordered by (term coverage,
     stars): how many distinct query terms the light fields *or the
     enrichment* cover, then popularity within each coverage tier — so
     for "machine learning framework python" pytorch's mined
     "Frameworks" category completes the 4-term tier its own fields
     can't reach.
  2. Dense — pgvector KNN over the halfvec expression index (inner
     product; embeddings are L2-normalised so the ordering equals
     cosine), against the configured storage label
     (config.MODEL_NAME / EMBEDDINGS_MODEL_LABEL — enriched vectors
     live under their own label per ADR 0006). pgvector 0.8 iterative
     scans keep filtered searches from coming back short.
  3. Name — pg_trgm exact / prefix / fuzzy on the repo name, so typos
     ("pytorhc") and half-remembered names still land (the fuzzy arm
     only fires for short queries; typos are short). Phase 2 adds the
     punctuation-normalised exact arm ("nextjs" == "next.js").

The lanes are fused with weighted Reciprocal Rank Fusion, then the
final ordering applies the additive blend (+ criticality term, dark by
default) and the exact-name-first rule.

The scoring math here intentionally mirrors ``ranking.py`` term by
term. The Python module is the source of truth for what the formula
*means*; this module is the source of truth for how it executes
efficiently in Postgres. They must stay in sync — any change to the
formula touches both files. The tests in ``tests/test_ranking.py`` pin
down the Python side; spot-checking against the SQL side is a manual
discipline.

Why compute the score in SQL rather than Python:
  - One round trip vs. two (fetch candidates, then re-fetch full rows).
  - Postgres can ORDER BY the computed score and LIMIT in one pass.
  - The same WHERE filters must apply to every lane *and* the re-rank;
    one statement removes the chance of those drifting.

See ADR 0018 (and 0013 for the history this supersedes).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

from .config import (
    DEMOTION_ARCHIVED,
    DEMOTION_FORK,
    DENSE_LANE_LIMIT,
    DEPENDENTS_PIVOT,
    EMBEDDING_DIM,
    FTS_COVERAGE_SLOTS,
    FTS_LANE_LIMIT,
    HNSW_EF_SEARCH,
    MODEL_NAME,
    NAME_FUZZY_MAX_TOKENS,
    NAME_LANE_LIMIT,
    PHASE2_RETRIEVAL,
    RECENCY_FLOOR,
    STARS_PIVOT_MAX,
    STARS_PIVOT_MIN,
    TRGM_SIMILARITY_THRESHOLD,
)
from .ranking import LaneWeights, ScoringWeights, normalise_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchFilters:
    """Optional filters applied as SQL WHERE conditions in every lane.

    Each field is None to mean "don't filter on this." Empty lists also
    mean "don't filter" — an explicit empty topic list shouldn't filter
    everything out.
    """
    language: Optional[str] = None       # exact match on primary_language
    topics: Optional[List[str]] = None   # any-of (array overlap)
    min_stars: Optional[int] = None
    exclude_archived: bool = True        # default on; matches indexer's pending query


@dataclass(frozen=True)
class SearchHit:
    """One result row, in final display order.

    The four ``*_contribution`` fields are the per-component additions
    to ``hybrid_score`` (already weight-multiplied and demotion-scaled),
    exposed so the UI can show *why* a result ranked where it did. Their
    sum equals ``hybrid_score``. ``similarity`` stays the raw cosine
    similarity for display (0.0 when the repo has no embedding — such
    repos are reachable through the lexical lanes).
    """
    repo_id: str
    full_name: str
    description: Optional[str]
    url: str
    primary_language: Optional[str]
    topics: List[str]
    stars: int
    pushed_at: object  # datetime, but typing it that way pulls in datetime import here
    similarity: float
    exact_name: bool
    hybrid_score: float
    similarity_contribution: float
    stars_contribution: float
    recency_contribution: float
    criticality_contribution: float


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

async def create_pool() -> asyncpg.Pool:
    """Create the shared connection pool. Uses the same DATABASE_URL env
    var as the crawler and indexer.

    ``statement_cache_size=0`` so the pool is safe behind either pooler
    mode — see ``crawler/src/db.py`` for the full reasoning.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set.")
    return await asyncpg.create_pool(
        dsn=dsn, min_size=2, max_size=10, statement_cache_size=0,
    )


# ---------------------------------------------------------------------------
# Search SQL
# ---------------------------------------------------------------------------

# Stage 1: build the tsqueries once, server-side; the main statement
# binds their *normalised text* as real tsquery parameters, so nothing
# re-parses query text per row.
#
#   q_and    — websearch semantics over the raw query (all terms, any
#              field including README).
#   q_or     — lane membership over the README-free light column. For
#              multi-term queries this is the OR of all pairwise ANDs
#              of the content lexemes ("at least two terms present"):
#              measured on the corpus, any-single-term membership for a
#              broad category query visits ~66K heap tuples (~3 s on
#              Micro compute) while the one-term tier never ranks
#              anyway; requiring two terms keeps the scan proportional
#              to the meaningful tiers. Single-term queries keep the
#              plain single-lexeme match. Stemming and stopwords come
#              from to_tsvector ("turn my notes into a website" reduces
#              to its content words). One blind spot: a websearch-
#              negated term appears as a positive lexeme here, so
#              negation queries rank, rather than filter, this tier.
#   q_any    — the OR of every content lexeme singly. The enrichment
#              arm probes with this (one GIN probe on the enrichment
#              table) because enrichment must contribute to *coverage*
#              even when it carries only one of the query's terms —
#              pytorch's mined "Frameworks" category is one term of
#              "machine learning framework python", and that one term
#              is the whole point (ADR 0020).
#   lexemes  — the first 8 content lexemes; Python binds each into its
#              own slot so the lane can count per-row term coverage
#              with plain @@ tests (no per-row parsing or ranking
#              functions).
_TSQUERY_SQL = """
WITH lex AS (
    SELECT replace(t.lexeme, '''', '') AS lexeme, t.ord
    FROM unnest(tsvector_to_array(to_tsvector('english', $1)))
         WITH ORDINALITY AS t(lexeme, ord)
    WHERE t.ord <= 8
)
SELECT websearch_to_tsquery('english', $1)::text AS q_and,
       COALESCE(CASE
           WHEN (SELECT count(*) FROM lex) >= 2 THEN
               (SELECT string_agg(
                    '(''' || a.lexeme || ''' & ''' || b.lexeme || ''')',
                    ' | ')
                FROM lex a JOIN lex b ON a.ord < b.ord)
           ELSE
               (SELECT string_agg('''' || lexeme || '''', ' | ') FROM lex)
       END, '') AS q_or,
       COALESCE(
           (SELECT string_agg('''' || lexeme || '''', ' | ') FROM lex),
           '') AS q_any,
       (SELECT COALESCE(array_agg(lexeme ORDER BY ord), '{}')
        FROM lex) AS lexemes
"""

# Stage 2: the lanes, the fusion, and the blend.
#
# Parameter slots (identical in both variants — see _build_search_sql):
#   $1  query vector (pgvector text format)         $2  model label
#   $3  fts lane limit    $4 dense lane limit       $5  name lane limit
#   $6  AND tsquery (normalised text -> tsquery)    $7  OR tsquery
#   $8  lowercased query for name matching          $9  LIKE prefix pattern
#   $10 language | NULL   $11 topics | NULL         $12 min_stars | NULL
#   $13 exclude_archived
#   $14 rrf_k   $15 w_full_text   $16 w_semantic    $17 w_name
#   $18 w_relevance   $19 w_stars   $20 w_recency   $21 half-life days
#   $22 final LIMIT
#   $23-$30 coverage slots (single-lexeme tsqueries, '' = unused)
#   $31 fuzzy name query ('' disables the trigram arm)
#   $32 any-single-lexeme OR tsquery (enrichment probe)
#   $33 punctuation-normalised name query | NULL
#   $34 w_criticality
#
# Constants baked in via .format (config values, not per-request):
# embedding dim, stars pivot clamp, recency floor, demotion factors,
# dependents pivot, and the dense lane's model label — a literal, not
# $2, so the per-label partial HNSW index (whose predicate the planner
# must match at plan time) is usable; $2 still serves the display-
# similarity join, which is a plain PK lookup.
_SEARCH_SQL_TEMPLATE = """
WITH {leading_ctes}fts_hits AS (
    -- Membership: any content lexeme in the light fields (name/topics/
    -- language/description) OR the full websearch query anywhere
    -- including the README (one bitmap-OR over two GIN probes; no
    -- weight labels, so no recheck detoasting){membership_doc}.
    -- Ordering: (term coverage, stars) — how many distinct query terms
    -- the light fields{coverage_doc} cover, then popularity within
    -- each tier. The coverage slots are pre-bound single-lexeme
    -- tsqueries (empty slots match nothing), so the per-row work is a
    -- few tsvector lookups on inline columns.
    SELECT repo_id, coverage, stars FROM (
{fts_inner}
        ORDER BY 3 DESC, 2 DESC
        LIMIT $3::int
    ) t
),
dense_hits AS (
    -- Must ORDER BY the exact expression the halfvec index is built
    -- on, or the planner falls back to a sequential scan.
    SELECT e.repo_id,
           (e.embedding::halfvec({dim})) <#> ($1::halfvec({dim})) AS dist
    FROM repository_embeddings e
    JOIN repositories r ON r.id = e.repo_id
    WHERE e.model_name = '{model_label}'
      AND ($10::text   IS NULL OR r.primary_language = $10)
      AND ($11::text[] IS NULL OR r.topics && $11::text[])
      AND ($12::int    IS NULL OR r.stars >= $12)
      AND ($13::bool = FALSE OR r.is_archived = FALSE)
    ORDER BY dist
    LIMIT $4::int
),
name_hits AS (
    SELECT repo_id, score, stars FROM (
        SELECT r.id AS repo_id, r.stars,
               GREATEST(
                   CASE WHEN lower(r.full_name) = $8
                          OR lower(r.name) = $8{name_norm_score_arm}
                        THEN 1.0 ELSE 0.0 END,
                   CASE WHEN lower(r.name) LIKE $9 THEN 0.7 ELSE 0.0 END,
                   similarity(r.name, $8)::float8
               ) AS score
        FROM repositories r
        WHERE (lower(r.full_name) = $8
               OR lower(r.name) LIKE $9{name_norm_where_arm}
               OR ($31::text <> '' AND r.name % $31))
          AND ($10::text   IS NULL OR r.primary_language = $10)
          AND ($11::text[] IS NULL OR r.topics && $11::text[])
          AND ($12::int    IS NULL OR r.stars >= $12)
          AND ($13::bool = FALSE OR r.is_archived = FALSE)
        ORDER BY 3 DESC, 2 DESC
        LIMIT $5::int
    ) t
),
fts AS (
    SELECT repo_id, ROW_NUMBER() OVER (
        ORDER BY coverage DESC, stars DESC, repo_id) AS rnk
    FROM fts_hits
),
dense AS (
    SELECT repo_id, ROW_NUMBER() OVER (ORDER BY dist, repo_id) AS rnk
    FROM dense_hits
),
name_lane AS (
    SELECT repo_id, ROW_NUMBER() OVER (
        ORDER BY score DESC, stars DESC, repo_id) AS rnk
    FROM name_hits
),
fused AS (
    SELECT COALESCE(f.repo_id, d.repo_id, n.repo_id) AS repo_id,
           $15::float8 * COALESCE(1.0 / ($14::float8 + f.rnk), 0.0)
         + $16::float8 * COALESCE(1.0 / ($14::float8 + d.rnk), 0.0)
         + $17::float8 * COALESCE(1.0 / ($14::float8 + n.rnk), 0.0) AS rrf
    FROM fts f
    FULL JOIN dense d ON d.repo_id = f.repo_id
    FULL JOIN name_lane n ON n.repo_id = COALESCE(f.repo_id, d.repo_id)
),
enriched AS (
    -- One pass joins display fields and computes the per-query
    -- normalisation inputs as window aggregates: the rrf range for
    -- min-max and the stars pivot (clamped geometric mean of candidate
    -- stars). A separate stats CTE + re-joins would visit every
    -- candidate's heap tuple three more times — measurable seconds on
    -- small compute.
    SELECT fu.repo_id, fu.rrf,
           r.full_name, r.name, r.description, r.url,
           r.primary_language, r.topics, r.stars, r.pushed_at,
           r.is_archived, r.is_fork,
           {dependent_select},
           MIN(fu.rrf) OVER () AS mn,
           MAX(fu.rrf) OVER () AS mx,
           LEAST({pivot_max}, GREATEST({pivot_min},
               EXP(AVG(LN(GREATEST(r.stars, 1))) OVER ()))) AS pivot
    FROM fused fu
    JOIN repositories r ON r.id = fu.repo_id{signals_join}
),
scored AS (
    SELECT *,
           {exact_name_expr} AS exact_name,
           CASE WHEN is_archived THEN {demote_archived}
                WHEN is_fork THEN {demote_fork}
                ELSE 1.0 END AS demotion,
           CASE WHEN mx > mn THEN (rrf - mn) / (mx - mn)
                ELSE 1.0 END AS rel_norm,
           stars / (stars + pivot) AS stars_sat,
           -- sat(dependents), fixed pivot (see config.DEPENDENTS_PIVOT
           -- and ranking.saturate_dependents): NULL and 0 both score 0.
           COALESCE(dependent_count, 0)::float8
               / (COALESCE(dependent_count, 0)::float8 + {dep_pivot})
               AS crit_sat,
           CASE WHEN pushed_at IS NULL THEN 0.0
                ELSE {recency_floor} + (1.0 - {recency_floor}) * EXP(
                    - GREATEST(
                        0,
                        EXTRACT(EPOCH FROM (NOW() - pushed_at)) / 86400.0
                    ) * LN(2) / $21::float8)
           END AS recency_norm
    FROM enriched
),
ranked AS (
    SELECT repo_id, full_name, description, url, primary_language,
           topics, stars, pushed_at, exact_name,
           (demotion * $18::float8 * rel_norm)     AS similarity_contribution,
           (demotion * $19::float8 * stars_sat)    AS stars_contribution,
           (demotion * $20::float8 * recency_norm) AS recency_contribution,
           (demotion * $34::float8 * crit_sat)     AS criticality_contribution,
           (demotion * ($18::float8 * rel_norm
                      + $19::float8 * stars_sat
                      + $20::float8 * recency_norm
                      + $34::float8 * crit_sat)) AS hybrid_score
    FROM scored
    ORDER BY exact_name DESC, hybrid_score DESC, stars DESC
    LIMIT $22::int
)
-- Display similarity (raw cosine) is fetched for the returned page
-- only, not the whole candidate pool.
SELECT rk.*,
       COALESCE(1 - (e.embedding <=> $1::vector), 0.0) AS similarity
FROM ranked rk
LEFT JOIN repository_embeddings e
       ON e.repo_id = rk.repo_id AND e.model_name = $2
ORDER BY rk.exact_name DESC, rk.hybrid_score DESC, rk.stars DESC
"""

# The two fts_hits inner shapes. Phase 1 is the ADR 0018 statement
# verbatim. Phase 2 (ADR 0020) folds repository_enrichment in through
# two bounded joins: arm one is phase 1's scan (materialized as
# fts_own) LEFT JOINed against coverage flags computed for exactly
# those candidates, and a second UNION ALL arm adds the repos
# reachable ONLY through enrichment (their own fields match nothing —
# the Doc2Query case), admitted by the strict pairwise bar. The NOT in
# arm two is what keeps the arms disjoint.
_FTS_INNER_PHASE1 = """\
        SELECT r.id AS repo_id, r.stars,
               {coverage_expr} AS coverage
        FROM repositories r
        WHERE (r.search_tsv_light @@ $7 OR r.search_tsv @@ $6)
          AND ($10::text   IS NULL OR r.primary_language = $10)
          AND ($11::text[] IS NULL OR r.topics && $11::text[])
          AND ($12::int    IS NULL OR r.stars >= $12)
          AND ($13::bool = FALSE OR r.is_archived = FALSE)"""

_FTS_INNER_PHASE2 = """\
        SELECT f.repo_id, f.stars,
               {coverage_arm1} AS coverage
        FROM fts_own f
        LEFT JOIN enrich_cov ec ON ec.repo_id = f.repo_id
        UNION ALL
        SELECT r.id AS repo_id, r.stars,
               {coverage_arm2} AS coverage
        FROM enrich_strict ec
        JOIN repositories r ON r.id = ec.repo_id
        WHERE NOT (r.search_tsv_light @@ $7 OR r.search_tsv @@ $6)
          AND ($10::text   IS NULL OR r.primary_language = $10)
          AND ($11::text[] IS NULL OR r.topics && $11::text[])
          AND ($12::int    IS NULL OR r.stars >= $12)
          AND ($13::bool = FALSE OR r.is_archived = FALSE)"""

# Phase 2's extra CTEs: per-repo enrichment term flags, read from the
# compact repository_enrichment_terms table (sql/0011) and bounded on
# both consumers. The original single CTE anchored on one GIN probe
# for ANY query lexeme ($32) over the source table; at awesome-mined
# scale (56K thin rows) that stayed cheap, but the full-corpus LLM
# pass made it 300K TOASTed-kilobyte rows and a common-word query
# ("python") heap-fetched tens of thousands of them — minutes of IO on
# small compute, measured 34s warm for the four-term gate query. What
# each consumer actually needs is narrow:
#
#   * arm one only ever reads flags for repos its OWN fields already
#     matched, so enrich_cov joins the materialized fts_own candidate
#     set (PK nested loop against inline-small term rows);
#   * arm two only admits repos whose enrichment covers the FULL query
#     ($6, websearch AND — measured: the pairwise bar $7 admits 40K
#     "members" from LLM text, where common word pairs are everywhere,
#     and the planner answers with a seq scan of repositories). The
#     Doc2Query premise argues for the whole-query bar anyway:
#     generated queries mirror complete user queries, so an
#     enrichment-only repo is exactly one whose enrichment contains
#     the query wholesale. The GIN intersection finds those few ids
#     selectively.
#
# The terms table folds a repo's rows into one lexeme-union tsvector,
# so per-slot flags are plain @@ tests (no aggregate); membership uses
# union-across-sources semantics (sql/0011 records the hair of extra
# width vs the old per-row bar). Missing terms rows (empty enrichment)
# -> empty CTEs -> the LEFT JOIN yields NULLs, arm two yields nothing,
# and the lane is exactly phase 1. params_anchor keeps $32 in the
# statement's inferred parameter list (same trick as phase 1's anchor)
# now that no live probe uses it.
def _phase2_ctes() -> str:
    flag_lines = ",\n".join(
        f"           (en.terms @@ ${n}) AS c{i}"
        for i, n in enumerate(range(23, 23 + FTS_COVERAGE_SLOTS), start=1)
    )
    own_bits = ",\n".join(
        f"           (r.search_tsv_light @@ ${n}) AS o{i}"
        for i, n in enumerate(range(23, 23 + FTS_COVERAGE_SLOTS), start=1)
    )
    own_sum = " + ".join(
        f"(r.search_tsv_light @@ ${n})::int"
        for n in range(23, 23 + FTS_COVERAGE_SLOTS)
    )
    return (
        "params_anchor AS (\n"
        "    SELECT $32::text AS q_any\n"
        "),\n"
        # The ORDER BY/LIMIT is a dominance bound: enrichment can raise
        # coverage by at most one (the LEAST(1, ...) cap), so ordering
        # by own-coverage+1 majorises every candidate's best possible
        # sort key, and only the top 2x lane-limit under that ordering
        # can reach the lane's top lane-limit. Flags then get computed
        # for hundreds of repos, not every FTS match (measured 12K for
        # a four-term query). A boundary-tier repo squeezed out by the
        # 2x slack would have entered the lane in its last ranks, where
        # RRF contribution is negligible; the dense lane still sees it.
        "fts_own AS MATERIALIZED (\n"
        "    SELECT r.id AS repo_id, r.stars,\n"
        f"{own_bits}\n"
        "    FROM repositories r\n"
        "    WHERE (r.search_tsv_light @@ $7 OR r.search_tsv @@ $6)\n"
        "      AND ($10::text   IS NULL OR r.primary_language = $10)\n"
        "      AND ($11::text[] IS NULL OR r.topics && $11::text[])\n"
        "      AND ($12::int    IS NULL OR r.stars >= $12)\n"
        "      AND ($13::bool = FALSE OR r.is_archived = FALSE)\n"
        f"    ORDER BY ({own_sum}) DESC, r.stars DESC\n"
        "    LIMIT $3::int * 2\n"
        "),\n"
        "enrich_cov AS (\n"
        "    SELECT en.repo_id,\n"
        f"{flag_lines}\n"
        "    FROM repository_enrichment_terms en\n"
        "    JOIN fts_own f ON f.repo_id = en.repo_id\n"
        "),\n"
        "enrich_strict AS (\n"
        "    SELECT en.repo_id,\n"
        f"{flag_lines}\n"
        "    FROM repository_enrichment_terms en\n"
        "    WHERE en.terms @@ $6\n"
        "),\n"
    )

# Phase 1 must still *bind* $32/$33 (a prepared statement's parameter
# list is inferred from the query text, and search() always sends 34
# arguments). This never-referenced CTE types them and costs nothing.
_PHASE1_PARAM_ANCHOR = """\
params_anchor AS (
    SELECT $32::text AS q_any, $33::text AS norm_name
),
"""

_EXACT_NAME_PHASE1 = "(lower(full_name) = $8 OR lower(name) = $8)"
# The normalised comparison is guarded so a NULL $33 (empty or
# multi-word query) yields FALSE, never NULL — exact_name feeds an
# ORDER BY ... DESC, where a NULL would sort above TRUE.
_EXACT_NAME_PHASE2 = """(lower(full_name) = $8 OR lower(name) = $8
               OR ($33::text IS NOT NULL
                   AND (translate(lower(full_name), '-._', '') = $33
                        OR translate(lower(name), '-._', '') = $33)))"""

_NAME_NORM_SCORE_ARM = """
                          OR translate(lower(r.full_name), '-._', '') = $33
                          OR translate(lower(r.name), '-._', '') = $33"""
_NAME_NORM_WHERE_ARM = """
               OR translate(lower(r.full_name), '-._', '') = $33
               OR translate(lower(r.name), '-._', '') = $33"""


def _coverage_expr(with_enrichment: bool) -> str:
    """The FTS lane's per-row term-coverage expression.

    Phase 1: how many of the query's first 8 content lexemes the light
    fields cover. Phase 2 adds enrichment, CAPPED at completing one
    additional term:

        coverage = own_terms + LEAST(1, enrichment_only_terms)

    The cap is the measured lesson of the first phase-2 eval: mined
    category trails paint every repo a topical list links with that
    list's vocabulary ("Game Engine Development" lands on bootstrap via
    one list's tooling section), and with stars ordering inside tiers,
    megastars with two stray enrichment terms took over category
    queries wholesale (-0.031 nDCG). Legitimate wins look different:
    the repo's own curated fields already cover most terms and
    enrichment supplies the one word its metadata lacks (pytorch:
    machine+learning+python own, "framework" mined). Letting enrichment
    complete a tier but never build one keeps the gate win and evicts
    the noise. Repos whose vocabulary is entirely synthetic (the pure
    Doc2Query case) still enter the lane through membership and reach
    ranking through the dense lane and RRF.
    """
    slots = range(23, 23 + FTS_COVERAGE_SLOTS)
    own = [f"(r.search_tsv_light @@ ${n})::int" for n in slots]
    if not with_enrichment:
        return ("\n             + ").join(own)
    extra = [
        f"(COALESCE(ec.c{i}, FALSE) AND NOT r.search_tsv_light @@ ${n})::int"
        for i, n in enumerate(slots, start=1)
    ]
    return (
        ("\n             + ").join(own)
        + "\n             + LEAST(1, "
        + ("\n                        + ").join(extra)
        + ")"
    )


def _coverage_expr_bits() -> str:
    """Arm one's coverage over fts_own's precomputed o1..oN bits.

    Same arithmetic as :func:`_coverage_expr` with enrichment, but the
    own-field probes were already evaluated inside the materialized
    candidate scan, so this reads booleans instead of re-running
    ``@@`` per output row.
    """
    idx = range(1, FTS_COVERAGE_SLOTS + 1)
    own = [f"(f.o{i})::int" for i in idx]
    extra = [
        f"(COALESCE(ec.c{i}, FALSE) AND NOT f.o{i})::int" for i in idx
    ]
    return (
        ("\n             + ").join(own)
        + "\n             + LEAST(1, "
        + ("\n                        + ").join(extra)
        + ")"
    )


def _build_search_sql(phase2: bool) -> str:
    """Render the search statement for one PHASE2_RETRIEVAL setting.

    Both variants bind the same 34 parameters and return the same
    columns (phase 1 hard-codes dependent_count NULL, so the
    criticality term is exactly 0 and hybrid_score is bit-identical to
    the ADR 0018 statement). That equivalence is what makes the flag a
    safe rollback lever *and* lets a flag-off deploy be verified as
    no-behavior-change before enrichment is switched on.
    """
    label = MODEL_NAME.replace("'", "''")
    if phase2:
        slots = dict(
            leading_ctes=_phase2_ctes(),
            membership_doc=(
                ", OR the whole websearch query inside one repo's "
                "enrichment terms (the UNION ALL arm)"
            ),
            coverage_doc=" or enrichment",
            fts_inner=_FTS_INNER_PHASE2.format(
                coverage_arm1=_coverage_expr_bits(),
                coverage_arm2=_coverage_expr(True),
            ),
            name_norm_score_arm=_NAME_NORM_SCORE_ARM,
            name_norm_where_arm=_NAME_NORM_WHERE_ARM,
            dependent_select="sg.dependent_count",
            signals_join=(
                "\n    LEFT JOIN repository_signals sg"
                " ON sg.repo_id = fu.repo_id"
            ),
            exact_name_expr=_EXACT_NAME_PHASE2,
        )
    else:
        slots = dict(
            leading_ctes=_PHASE1_PARAM_ANCHOR,
            membership_doc="",
            coverage_doc="",
            fts_inner=_FTS_INNER_PHASE1.format(
                coverage_expr=_coverage_expr(False)
            ),
            name_norm_score_arm="",
            name_norm_where_arm="",
            dependent_select="NULL::int AS dependent_count",
            signals_join="",
            exact_name_expr=_EXACT_NAME_PHASE1,
        )
    return _SEARCH_SQL_TEMPLATE.format(
        dim=EMBEDDING_DIM,
        model_label=label,
        pivot_min=STARS_PIVOT_MIN,
        pivot_max=STARS_PIVOT_MAX,
        dep_pivot=DEPENDENTS_PIVOT,
        recency_floor=RECENCY_FLOOR,
        demote_archived=DEMOTION_ARCHIVED,
        demote_fork=DEMOTION_FORK,
        **slots,
    )


_SEARCH_SQL = _build_search_sql(PHASE2_RETRIEVAL)


def _vector_to_pg(vector: List[float]) -> str:
    """Format a vector for pgvector's text input syntax.

    Matches ``indexer/pipeline/db._vector_to_pg`` byte-for-byte. We
    duplicate it (rather than import) to keep this service installable
    without the indexer package on the path. The format is fixed by
    pgvector's wire protocol; both sides will agree mechanically.
    """
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def name_query(query: str) -> str:
    """The normalised form used for exact/fuzzy name matching."""
    return query.strip().lower()


def like_prefix_pattern(query: str) -> str:
    """Left-anchored LIKE pattern for name-prefix matching, with LIKE
    metacharacters escaped. An empty query yields an empty pattern,
    which matches no (non-empty) name."""
    q = name_query(query)
    if not q:
        return ""
    return re.sub(r"([\\%_])", r"\\\1", q) + "%"


def fuzzy_name_query(query: str) -> str:
    """The string the trigram arm matches against, or '' to disable it.
    Fuzzy matching exists for typo'd names, which are short; running
    trigram similarity for a whole sentence against every repo name is
    seconds of work for no recall (an empty string has no trigrams, so
    `%` matches nothing — the cheap off-switch)."""
    q = name_query(query)
    return q if q and len(q.split()) <= NAME_FUZZY_MAX_TOKENS else ""


def normalized_name_query(query: str) -> Optional[str]:
    """$33: the punctuation-normalised form for exact-name matching
    ("nextjs" == "next.js"), or None to disable the arm. None rather
    than '' because a repo named only punctuation ("...") normalises to
    the empty string and must not exact-match an emptyish query; NULL
    comparisons are never true. Multi-word queries keep the arm off —
    names don't contain spaces, and normalisation only strips [-._]."""
    q = normalise_name(query)
    return q if q and " " not in query.strip() else None


def coverage_slots(lexemes: List[str]) -> List[str]:
    """Bind-ready single-lexeme tsquery texts for the coverage slots:
    the first FTS_COVERAGE_SLOTS content lexemes, quoted, padded with
    empty tsqueries (which match nothing).

    Coverage counts a term present in ANY light field (name, topics,
    language, description). A topics/language-only restriction
    (':B'-labelled slots) was tried and reverted: it promotes repos
    whose topics happen to enumerate every query word — for "machine
    learning framework python" the topic-complete tier is 49 repos of
    mostly minor frameworks, which pushed tensorflow (topics lack
    "framework"; description has it) out of the top ten entirely. The
    eval gate was measured on any-field coverage; this stays what was
    measured."""
    slots = [
        "'" + lexeme.replace("'", "") + "'"
        for lexeme in lexemes[:FTS_COVERAGE_SLOTS]
    ]
    return slots + [""] * (FTS_COVERAGE_SLOTS - len(slots))


# pgvector 0.8 added hnsw.iterative_scan; on older servers the SET
# would error, so probe once per process. The probe reads the installed
# extension version from the catalog — asking current_setting() would
# lie behind a pooler, where a fresh backend hasn't loaded the vector
# library yet and reports extension GUCs as undefined.
_iterative_scan_available: Optional[bool] = None


async def _probe_iterative_scan(conn: asyncpg.Connection) -> bool:
    global _iterative_scan_available
    if _iterative_scan_available is None:
        version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        try:
            major, minor = (int(part) for part in version.split(".")[:2])
            _iterative_scan_available = (major, minor) >= (0, 8)
        except (AttributeError, ValueError):
            _iterative_scan_available = False
        if not _iterative_scan_available:
            logger.info(
                "pgvector %s lacks hnsw.iterative_scan; filtered dense "
                "lanes may run shallow", version,
            )
    return _iterative_scan_available


async def search(
    pool: asyncpg.Pool,
    *,
    query: str,
    query_vector: List[float],
    filters: SearchFilters,
    weights: ScoringWeights,
    lanes: LaneWeights = LaneWeights(),
    limit: int,
) -> List[SearchHit]:
    """Run the three-lane hybrid search and return ranked hits."""

    query_vec_text = _vector_to_pg(query_vector)

    async with pool.acquire() as conn:
        use_iterative = await _probe_iterative_scan(conn)
        # Session GUCs are set LOCAL inside a transaction so they scope
        # to this query only — required behind the transaction pooler,
        # where plain SETs don't stick to the next statement anyway.
        async with conn.transaction():
            # One round trip for all the session GUCs (argument-free
            # execute uses the simple protocol, which allows multiple
            # statements). search_path: pg_trgm lives in `extensions`
            # on Supabase and `public` locally; listing both resolves
            # `%`/similarity() everywhere (missing schemas in a
            # search_path are ignored).
            gucs = [
                "SET LOCAL search_path = public, extensions",
                # Generic plans can't fold the disabled name-lane arms
                # ('' fuzzy query, NULL normalised name) or use the
                # LIKE prefix, so they answer the OR chain with a 2s+
                # parallel seq scan of repositories — on every query.
                # Custom plans see the actual values; planning is ~8ms.
                "SET LOCAL plan_cache_mode = force_custom_plan",
                f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}",
                f"SET LOCAL pg_trgm.similarity_threshold = "
                f"{TRGM_SIMILARITY_THRESHOLD}",
            ]
            if use_iterative:
                gucs.append("SET LOCAL hnsw.iterative_scan = relaxed_order")
            await conn.execute("; ".join(gucs))
            tsq = await conn.fetchrow(_TSQUERY_SQL, query)
            rows = await conn.fetch(
                _SEARCH_SQL,
                query_vec_text,                 # $1
                MODEL_NAME,                     # $2
                FTS_LANE_LIMIT,                 # $3
                DENSE_LANE_LIMIT,               # $4
                NAME_LANE_LIMIT,                # $5
                tsq["q_and"],                   # $6
                tsq["q_or"],                    # $7
                name_query(query),              # $8
                like_prefix_pattern(query),     # $9
                filters.language,               # $10
                filters.topics if filters.topics else None,  # $11
                filters.min_stars,              # $12
                filters.exclude_archived,       # $13
                float(lanes.rrf_k),             # $14
                lanes.full_text,                # $15
                lanes.semantic,                 # $16
                lanes.name,                     # $17
                weights.similarity,             # $18
                weights.stars,                  # $19
                weights.recency,                # $20
                weights.half_life_days,         # $21
                limit,                          # $22
                *coverage_slots(list(tsq["lexemes"] or [])),  # $23-$30
                fuzzy_name_query(query),        # $31
                tsq["q_any"],                   # $32
                normalized_name_query(query),   # $33
                weights.criticality,            # $34
            )

    return [
        SearchHit(
            repo_id=row["repo_id"],
            full_name=row["full_name"],
            description=row["description"],
            url=row["url"],
            primary_language=row["primary_language"],
            topics=list(row["topics"]) if row["topics"] else [],
            stars=row["stars"],
            pushed_at=row["pushed_at"],
            similarity=float(row["similarity"]),
            exact_name=bool(row["exact_name"]),
            hybrid_score=float(row["hybrid_score"]),
            similarity_contribution=float(row["similarity_contribution"]),
            stars_contribution=float(row["stars_contribution"]),
            recency_contribution=float(row["recency_contribution"]),
            criticality_contribution=float(row["criticality_contribution"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Usage guides (see ADR 0016)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoForGuide:
    """The fields the guide generator needs about one repo."""
    repo_id: str
    full_name: str
    description: Optional[str]
    url: str
    primary_language: Optional[str]
    topics: List[str]
    readme: Optional[str]
    readme_fetched_at: object  # datetime or None


@dataclass(frozen=True)
class CachedGuide:
    guide: str
    model_name: str
    source_readme_fetched_at: object  # datetime or None


_FETCH_REPO_FOR_GUIDE_SQL = """
SELECT id, full_name, description, url, primary_language, topics,
       readme, readme_fetched_at
FROM repositories
WHERE id = $1
"""


async def fetch_repo_for_guide(
    pool: asyncpg.Pool, repo_id: str
) -> Optional[RepoForGuide]:
    """Return the repo's display fields + README, or None if it doesn't exist."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FETCH_REPO_FOR_GUIDE_SQL, repo_id)
    if row is None:
        return None
    return RepoForGuide(
        repo_id=row["id"],
        full_name=row["full_name"],
        description=row["description"],
        url=row["url"],
        primary_language=row["primary_language"],
        topics=list(row["topics"]) if row["topics"] else [],
        readme=row["readme"],
        readme_fetched_at=row["readme_fetched_at"],
    )


_GET_GUIDE_SQL = """
SELECT guide, model_name, source_readme_fetched_at
FROM repository_guides
WHERE repo_id = $1
"""


async def get_cached_guide(
    pool: asyncpg.Pool, repo_id: str
) -> Optional[CachedGuide]:
    """Return the cached guide for ``repo_id``, or None on a cache miss."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_GUIDE_SQL, repo_id)
    if row is None:
        return None
    return CachedGuide(
        guide=row["guide"],
        model_name=row["model_name"],
        source_readme_fetched_at=row["source_readme_fetched_at"],
    )


_UPSERT_GUIDE_SQL = """
INSERT INTO repository_guides
    (repo_id, guide, model_name, source_readme_fetched_at, generated_at)
VALUES ($1, $2, $3, $4, NOW())
ON CONFLICT (repo_id) DO UPDATE SET
    guide                    = EXCLUDED.guide,
    model_name               = EXCLUDED.model_name,
    source_readme_fetched_at = EXCLUDED.source_readme_fetched_at,
    generated_at             = NOW()
"""


async def upsert_guide(
    pool: asyncpg.Pool,
    repo_id: str,
    guide: str,
    model_name: str,
    source_readme_fetched_at: object,
) -> None:
    """Store (or replace) the cached guide for ``repo_id``."""
    async with pool.acquire() as conn:
        await conn.execute(
            _UPSERT_GUIDE_SQL, repo_id, guide, model_name, source_readme_fetched_at
        )
