"""Hybrid scoring for search results.

This module is pure (no I/O, no async): given numeric inputs, return a
score. Mirrors the layout of ``indexer/pipeline/document_builder.py`` —
the ranking math is small but disproportionately important to search
quality, so it lives in its own well-tested module rather than being
inlined into the SQL query string.

The scoring formula and its rationale are documented in ADR 0013. The
short version: each component (similarity, stars, recency) is normalised
to roughly [0, 1] so the weights are interpretable, then combined as a
weighted sum.

The actual top-K query computes these expressions in SQL (see ``db.py``).
The Python implementations here exist for:
  - testing the math in isolation,
  - documenting it in one place,
  - re-ranking in Python if a future caller ever needs to (e.g., to
    tune weights without re-issuing the SQL query).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Normalisation knobs
# ---------------------------------------------------------------------------

# Denominator for log-stars normalisation.
#
# We want stars_norm in [0, 1] for the realistic range of repos in our
# corpus. The most-starred public repo on GitHub is ~400K stars
# (freeCodeCamp/freeCodeCamp). log10(1 + 400_000) ≈ 5.60. We round up to
# 6.0 so the normalised value stays under 1.0 even as new megastar repos
# emerge — and a fixed denominator means we don't need a per-query
# `MAX(stars)` lookup. See ADR 0013.
LOG_STARS_DENOMINATOR: float = 6.0

# Recency half-life in days.
#
# A repo pushed `HALF_LIFE_DAYS` ago scores 0.5 on the recency component;
# 2*half-life ago scores 0.25; etc. 365 days is a reasonable default for
# "still maintained". Tunable via config.
DEFAULT_RECENCY_HALF_LIFE_DAYS: float = 365.0


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

# Similarity dominates: this is a *semantic* search engine, not a
# popularity ranker. Stars and recency are tie-breakers and demote
# semantically-close-but-stale or low-signal matches.
DEFAULT_SIMILARITY_WEIGHT: float = 1.0
DEFAULT_STARS_WEIGHT: float = 0.3
DEFAULT_RECENCY_WEIGHT: float = 0.2


@dataclass(frozen=True)
class ScoringWeights:
    """Configuration for the hybrid score.

    Held as a small value object so it's trivially passable across the
    module boundary (e.g., from server config or per-request overrides
    into the SQL builder).
    """
    similarity: float = DEFAULT_SIMILARITY_WEIGHT
    stars: float = DEFAULT_STARS_WEIGHT
    recency: float = DEFAULT_RECENCY_WEIGHT
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS


# ---------------------------------------------------------------------------
# Pure scoring functions
# ---------------------------------------------------------------------------

def normalise_stars(stars: int) -> float:
    """Normalise a star count to roughly [0, 1].

    log10(1 + stars) / LOG_STARS_DENOMINATOR. The +1 keeps it well-defined
    for stars=0 (which we don't expect in our corpus, but be defensive).

    Examples:
        100 stars   -> 0.335
        1_000       -> 0.500
        10_000      -> 0.667
        100_000     -> 0.835
        400_000     -> 0.933
    """
    if stars < 0:
        # Garbage in -> 0; never raise from a scoring function.
        return 0.0
    return math.log10(1 + stars) / LOG_STARS_DENOMINATOR


def normalise_recency(
    pushed_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Normalise "time since last push" to [0, 1] via half-life decay.

    A repo pushed *now* scores 1.0; one half-life ago scores 0.5; two
    half-lives ago, 0.25; etc. ``pushed_at`` of None scores 0 — repos
    that have never been pushed are treated as maximally stale.

    Implementation: ``0.5 ** (age_days / half_life_days)``, equivalent
    to ``exp(-age_days * ln(2) / half_life_days)``. The "half-life"
    framing is intuitive for tuning ("a year ago scores half"); we
    use it directly so the parameter name matches the math.

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
    return 0.5 ** (age_days / half_life_days)


def hybrid_score(
    *,
    similarity: float,
    stars: int,
    pushed_at: Optional[datetime],
    weights: ScoringWeights = ScoringWeights(),
    now: Optional[datetime] = None,
) -> float:
    """Combine the three components into a single hybrid score.

    All inputs are normalised first (see ADR 0013), then summed with the
    provided weights. The output is roughly bounded by
    ``weights.similarity + weights.stars + weights.recency`` but its
    absolute scale doesn't matter — only the *ordering* across results
    is meaningful.
    """
    sim_norm = max(0.0, min(1.0, similarity))  # clamp pathological values
    stars_norm = normalise_stars(stars)
    recency_norm = normalise_recency(
        pushed_at, now=now, half_life_days=weights.half_life_days
    )

    return (
        weights.similarity * sim_norm
        + weights.stars * stars_norm
        + weights.recency * recency_norm
    )
