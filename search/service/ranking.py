"""Ranking math for search results: RRF fusion + the additive blend.

This module is pure (no I/O, no async): given numeric inputs, return a
score. The ranking math is small but disproportionately important to
search quality, so it lives in its own well-tested module rather than
being inlined into the SQL query string.

The formula and its rationale are documented in ADR 0018 (which
supersedes ADR 0013's similarity-weighted sum). The short version:

  1. Three retrieval lanes (full-text, dense, name) each produce a
     ranking; weighted Reciprocal Rank Fusion merges them into one
     relevance score per candidate:  sum_lane  w_lane / (k + rank).
  2. The final ordering is an additive blend of normalised components:
         final = demotion * ( w_rel * minmax(rrf over candidates)
                            + w_stars * sat(stars)
                            + w_rec * recency )
     with sat(x) = x / (x + pivot) — saturation, so popularity is a
     bounded boost, never a runaway multiplier — and recency floored,
     so finished-but-canonical libraries don't sink to zero.
  3. crates.io's exact-name rule: a candidate whose name (or
     owner/name) exactly matches the query sorts above everything,
     popularity-independent. If you type a thing's name, you get it.

The actual top-K query computes these expressions in SQL (see
``db.py``). The Python implementations here exist for testing the math
in isolation, documenting it in one place, and re-ranking in Python if
a future caller ever needs to. They must stay in sync with the SQL —
any change to the formula touches both files; the tests in
``tests/test_ranking.py`` pin down the Python side.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence

from .config import (
    DEFAULT_CRITICALITY_WEIGHT,
    DEMOTION_ARCHIVED,
    DEMOTION_FORK,
    DEPENDENTS_PIVOT,
    FULL_TEXT_WEIGHT,
    NAME_WEIGHT,
    RECENCY_FLOOR,
    RRF_K,
    SEMANTIC_WEIGHT,
    STARS_PIVOT_MAX,
    STARS_PIVOT_MIN,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Recency half-life in days: the decaying part of the recency signal
# halves every this-many days (but never sinks below RECENCY_FLOOR).
DEFAULT_RECENCY_HALF_LIFE_DAYS: float = 365.0

# Relevance dominates: this is a search engine, not a popularity
# ranker. Stars and recency are tie-breakers among comparably relevant
# results. The field names date from when the relevance signal was
# cosine similarity alone; they're kept because the public API and the
# frontend sliders speak them.
DEFAULT_SIMILARITY_WEIGHT: float = 1.0
DEFAULT_STARS_WEIGHT: float = 0.3
DEFAULT_RECENCY_WEIGHT: float = 0.2


@dataclass(frozen=True)
class ScoringWeights:
    """Blend weights (per-request overridable; the frontend sliders map
    onto the first three fields).

    ``similarity`` weights the fused *relevance* signal — the name is
    the API's historical vocabulary, kept so existing clients and the
    tune-ranking sliders keep working unchanged.

    ``criticality`` weights sat(deps.dev dependents). It defaults to
    0.0 — dark until the eval measures a win (ADR 0020); the SQL and
    the response schema carry it either way so sweeps are pure
    per-request config.
    """
    similarity: float = DEFAULT_SIMILARITY_WEIGHT
    stars: float = DEFAULT_STARS_WEIGHT
    recency: float = DEFAULT_RECENCY_WEIGHT
    criticality: float = DEFAULT_CRITICALITY_WEIGHT
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS


@dataclass(frozen=True)
class LaneWeights:
    """RRF fusion configuration (per-request overridable for eval
    sweeps; requests normally ride the config defaults)."""
    full_text: float = FULL_TEXT_WEIGHT
    semantic: float = SEMANTIC_WEIGHT
    name: float = NAME_WEIGHT
    rrf_k: int = RRF_K


# ---------------------------------------------------------------------------
# Pure scoring functions
# ---------------------------------------------------------------------------

def rrf_score(
    ranks: Dict[str, Optional[int]],
    lanes: LaneWeights = LaneWeights(),
) -> float:
    """Weighted Reciprocal Rank Fusion over per-lane ranks (1-based).

    ``ranks`` maps lane name ('full_text' | 'semantic' | 'name') to the
    candidate's rank in that lane, or None if the lane didn't return it.
    """
    weights = {
        "full_text": lanes.full_text,
        "semantic": lanes.semantic,
        "name": lanes.name,
    }
    total = 0.0
    for lane, rank in ranks.items():
        if rank is not None:
            total += weights[lane] / (lanes.rrf_k + rank)
    return total


def minmax_norm(value: float, lo: float, hi: float) -> float:
    """Normalise ``value`` into [0, 1] given the candidate set's range.
    A degenerate range (single candidate) maps to 1.0 — that candidate
    IS the most relevant thing we found."""
    if hi <= lo:
        return 1.0
    return (value - lo) / (hi - lo)


def stars_pivot(candidate_stars: Sequence[int]) -> float:
    """The saturation pivot: geometric mean of the candidate set's star
    counts, clamped to a sane band (config: STARS_PIVOT_MIN/MAX).

    Per-query, so "typical for this query's candidates" is the yardstick
    — sat(pivot) = 0.5 by construction. Zero-star rows count as 1 star
    to keep the log defined.
    """
    if not candidate_stars:
        return STARS_PIVOT_MIN
    mean_log = sum(math.log(max(s, 1)) for s in candidate_stars) / len(candidate_stars)
    return min(STARS_PIVOT_MAX, max(STARS_PIVOT_MIN, math.exp(mean_log)))


def saturate_stars(stars: int, pivot: float) -> float:
    """sat(x) = x / (x + pivot): 0 at zero stars, 0.5 at the pivot,
    asymptotically 1. Saturation caps rich-get-richer — at pivot=1000,
    100K stars scores 0.990 and 10K scores 0.909: distinguishable, but
    megastardom can't drown relevance."""
    if stars <= 0:
        return 0.0
    return stars / (stars + pivot)


def saturate_dependents(
    dependent_count: Optional[int], pivot: float = DEPENDENTS_PIVOT
) -> float:
    """sat(dependents) with a fixed pivot (config: DEPENDENTS_PIVOT).

    Fixed rather than per-candidate-set because most candidates have no
    published package at all — a geometric mean over a mostly-empty
    signal is noise, not a yardstick. None (no package known) and 0
    (package nobody depends on) both score 0.0; the distinction is
    provenance, not rank.
    """
    if dependent_count is None or dependent_count <= 0:
        return 0.0
    return dependent_count / (dependent_count + pivot)


def normalise_recency(
    pushed_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
    floor: float = RECENCY_FLOOR,
) -> float:
    """Bounded maintenance signal in [floor, 1] via floored half-life
    decay: ``floor + (1 - floor) * 0.5 ** (age_days / half_life)``.

    A repo pushed now scores 1.0; one half-life ago, the midpoint of the
    band; long-abandoned ones approach the floor rather than zero — old
    canonical libraries must not sink out of sight for being finished.
    ``pushed_at`` of None scores 0 (below the floor deliberately:
    "never pushed" is a data anomaly, not a maintenance statement).

    ``now`` is injectable for deterministic testing.
    """
    if pushed_at is None:
        return 0.0

    if now is None:
        now = datetime.now(timezone.utc)

    # Be defensive about timezone-naive inputs. The crawler stores
    # TIMESTAMPTZ so we always *should* get aware datetimes from asyncpg,
    # but a stray naive datetime here would silently break math.
    if pushed_at.tzinfo is None:
        pushed_at = pushed_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_days = max(0.0, (now - pushed_at).total_seconds() / 86_400.0)
    return floor + (1.0 - floor) * 0.5 ** (age_days / half_life_days)


def demotion_factor(*, is_archived: bool, is_fork: bool) -> float:
    """Multiplier applied to the whole blended score. Archived beats
    fork if somehow both apply — an archived fork is mostly archive."""
    if is_archived:
        return DEMOTION_ARCHIVED
    if is_fork:
        return DEMOTION_FORK
    return 1.0


_NAME_PUNCT_RE = re.compile(r"[-._]")


def normalise_name(text: str) -> str:
    """Punctuation-normalised name form: lowercase, [-._] removed —
    "Next.js", "next-js", and "nextjs" all become "nextjs". Mirrors the
    SQL ``translate(lower(x), '-._', '')`` (indexes in migration 0009)
    exactly; both sides must keep the same character set."""
    return _NAME_PUNCT_RE.sub("", text.strip().lower())


def is_exact_name(query: str, *, full_name: str, name: str) -> bool:
    """crates.io's rule: the query IS this repo's name. Case-insensitive
    on either the bare name ("pytorch") or owner/name ("pytorch/pytorch").

    Phase 2 extends the match to the punctuation-normalised forms, so
    "nextjs" pins vercel/next.js into the exact tier alongside any repo
    literally named "nextjs" — inside that tier, score-then-stars
    ordering puts the canonical repo first (the ADR 0018 name-squatter
    landmine). Normalisation never *removes* a match: the plain
    comparison is a subset of the normalised one.
    """
    q = query.strip().lower()
    if not q:
        return False
    if full_name.lower() == q or name.lower() == q:
        return True
    nq = normalise_name(query)
    return bool(nq) and (
        normalise_name(full_name) == nq or normalise_name(name) == nq
    )


def hybrid_score(
    *,
    rrf: float,
    rrf_min: float,
    rrf_max: float,
    stars: int,
    pivot: float,
    pushed_at: Optional[datetime],
    dependent_count: Optional[int] = None,
    is_archived: bool = False,
    is_fork: bool = False,
    weights: ScoringWeights = ScoringWeights(),
    now: Optional[datetime] = None,
) -> float:
    """The full blend, mirroring the SQL in ``db.py`` term for term.
    Only the *ordering* across results is meaningful, with one caveat:
    relevance is min-max normalised over the query's candidate set, so
    scores are comparable within a response, not across queries."""
    rel_norm = minmax_norm(rrf, rrf_min, rrf_max)
    stars_sat = saturate_stars(stars, pivot)
    recency_norm = normalise_recency(
        pushed_at, now=now, half_life_days=weights.half_life_days
    )
    crit_sat = saturate_dependents(dependent_count)
    return demotion_factor(is_archived=is_archived, is_fork=is_fork) * (
        weights.similarity * rel_norm
        + weights.stars * stars_sat
        + weights.recency * recency_norm
        + weights.criticality * crit_sat
    )
