"""Tests for the hybrid scoring math.

Pure module → pure tests. The cases pin down behaviour that's easy to
break silently when someone "tweaks the formula": ordering between
component scales, edge cases at the bounds, and the effect of weights
on tie-breaking.

If a failure here is mysterious, also re-read ADR 0013 — these tests
encode the same intent as the ADR, in executable form.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from service.ranking import (
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    LOG_STARS_DENOMINATOR,
    ScoringWeights,
    hybrid_score,
    normalise_recency,
    normalise_stars,
)


# ---------------------------------------------------------------------------
# normalise_stars
# ---------------------------------------------------------------------------

def test_zero_stars_is_zero():
    """log10(1) / denom == 0. Defensive: corpus is ≥200 stars but never
    assume the input."""
    assert normalise_stars(0) == 0.0


def test_negative_stars_is_zero_not_an_exception():
    """Garbage in -> 0; scoring never raises. Comment in the function
    spells this out — pin it down so future refactors don't change it."""
    assert normalise_stars(-1) == 0.0


def test_stars_normalisation_is_monotonic():
    """More stars -> higher normalised value. The whole point."""
    values = [normalise_stars(s) for s in [0, 100, 1_000, 10_000, 100_000]]
    assert values == sorted(values)


def test_stars_normalisation_within_unit_range_for_realistic_corpus():
    """Up to GitHub's largest repo (~400K stars), we stay below 1.0."""
    assert 0.0 <= normalise_stars(100) < 1.0
    assert 0.0 <= normalise_stars(400_000) < 1.0


def test_stars_normalisation_exact_values():
    """Pin down the curve. If LOG_STARS_DENOMINATOR ever changes, this
    test fails loudly — which is the intent: it's a deliberate decision,
    document the new values in the ADR."""
    # log10(1 + 999) / 6.0 = log10(1000) / 6.0 = 3.0/6.0 = 0.5
    assert normalise_stars(999) == 0.5
    # log10(1 + 99) / 6.0 ≈ 2.0 / 6.0 ≈ 0.3333
    assert math.isclose(normalise_stars(99), 1 / 3, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# normalise_recency
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_recency_just_pushed_is_one():
    assert math.isclose(normalise_recency(NOW, now=NOW), 1.0)


def test_recency_one_half_life_ago_is_half():
    pushed = NOW - timedelta(days=DEFAULT_RECENCY_HALF_LIFE_DAYS)
    assert math.isclose(normalise_recency(pushed, now=NOW), 0.5, rel_tol=1e-9)


def test_recency_two_half_lives_ago_is_quarter():
    pushed = NOW - timedelta(days=2 * DEFAULT_RECENCY_HALF_LIFE_DAYS)
    assert math.isclose(normalise_recency(pushed, now=NOW), 0.25, rel_tol=1e-9)


def test_recency_none_is_zero():
    """Repos with no pushed_at score zero — treated as maximally stale.
    Not every repo has been pushed (rare; the GraphQL field is nullable)."""
    assert normalise_recency(None, now=NOW) == 0.0


def test_recency_handles_naive_datetime_defensively():
    """asyncpg with TIMESTAMPTZ should always return aware datetimes,
    but a stray naive input shouldn't silently corrupt the math."""
    pushed_naive = datetime(2025, 5, 1, 12, 0, 0)  # no tzinfo
    # Treat as UTC; one year ago with 365-day half-life -> 0.5
    result = normalise_recency(pushed_naive, now=NOW)
    assert math.isclose(result, 0.5, rel_tol=1e-3)


def test_recency_future_pushed_at_clamps_to_one():
    """Clock skew or bad data — pushed_at in the future shouldn't
    produce >1 and skew the leaderboard."""
    future = NOW + timedelta(days=10)
    result = normalise_recency(future, now=NOW)
    assert result == 1.0


def test_recency_custom_half_life():
    """Half-life is configurable via weights; the function honours it."""
    pushed = NOW - timedelta(days=30)
    result = normalise_recency(pushed, now=NOW, half_life_days=30)
    assert math.isclose(result, 0.5, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# hybrid_score
# ---------------------------------------------------------------------------

def test_hybrid_score_combines_components_with_default_weights():
    """A spot-check: similarity 0.8, stars 1000, pushed today.
       sim_norm   = 0.8
       stars_norm = log10(1001)/6 ≈ 0.5005
       rec_norm   = 1.0
       hybrid     = 1.0*0.8 + 0.3*0.5005 + 0.2*1.0 ≈ 1.1502
    """
    s = hybrid_score(similarity=0.8, stars=1000, pushed_at=NOW, now=NOW)
    expected = (
        1.0 * 0.8
        + 0.3 * (math.log10(1001) / LOG_STARS_DENOMINATOR)
        + 0.2 * 1.0
    )
    assert math.isclose(s, expected, rel_tol=1e-9)


def test_hybrid_score_similarity_dominates_with_default_weights():
    """The whole point of the search engine: a highly-relevant low-star
    repo should beat a low-relevance megastar repo. Sanity-check this
    with the defaults; if someone changes weights such that this no
    longer holds, that's a search-quality regression."""
    relevant_small = hybrid_score(
        similarity=0.85, stars=200, pushed_at=NOW, now=NOW,
    )
    irrelevant_huge = hybrid_score(
        similarity=0.40, stars=400_000, pushed_at=NOW, now=NOW,
    )
    assert relevant_small > irrelevant_huge


def test_hybrid_score_breaks_similarity_ties_by_stars_and_recency():
    """When similarity is identical, the popular and recent repo wins.
    Stars and recency are explicitly designed as tie-breakers."""
    a = hybrid_score(similarity=0.7, stars=10_000, pushed_at=NOW, now=NOW)
    b = hybrid_score(
        similarity=0.7,
        stars=300,
        pushed_at=NOW - timedelta(days=5 * 365),  # ancient
        now=NOW,
    )
    assert a > b


def test_hybrid_score_clamps_pathological_similarity():
    """If pgvector hands us a weird value (numerical noise pushing
    similarity slightly above 1, or below 0), we should clamp rather
    than let it propagate."""
    s_high = hybrid_score(similarity=1.5, stars=0, pushed_at=None, now=NOW)
    s_one = hybrid_score(similarity=1.0, stars=0, pushed_at=None, now=NOW)
    assert s_high == s_one

    s_low = hybrid_score(similarity=-0.1, stars=0, pushed_at=None, now=NOW)
    s_zero = hybrid_score(similarity=0.0, stars=0, pushed_at=None, now=NOW)
    assert s_low == s_zero


def test_hybrid_score_zero_weights_disables_a_component():
    """Setting a weight to 0 effectively turns off that component.
    This is the recommended way to A/B between pure-similarity and
    hybrid: ?weights.stars=0&weights.recency=0."""
    pure_sim_weights = ScoringWeights(similarity=1.0, stars=0.0, recency=0.0)
    s = hybrid_score(
        similarity=0.5,
        stars=400_000,
        pushed_at=NOW,
        weights=pure_sim_weights,
        now=NOW,
    )
    assert math.isclose(s, 0.5, rel_tol=1e-9)


def test_hybrid_score_no_pushed_at_doesnt_break_anything():
    """Some repos have NULL pushed_at; recency_norm becomes 0 and the
    score is just similarity + stars contributions."""
    s = hybrid_score(similarity=0.6, stars=1000, pushed_at=None, now=NOW)
    expected = (
        1.0 * 0.6
        + 0.3 * (math.log10(1001) / LOG_STARS_DENOMINATOR)
        + 0.2 * 0.0
    )
    assert math.isclose(s, expected, rel_tol=1e-9)
