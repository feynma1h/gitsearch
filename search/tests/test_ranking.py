"""Tests for the ranking math (RRF fusion + additive blend).

Pure module → pure tests. The cases pin down behaviour that's easy to
break silently when someone "tweaks the formula": ordering between
component scales, edge cases at the bounds, the exact-name rule, and
the effect of weights on tie-breaking.

If a failure here is mysterious, also re-read ADR 0018 — these tests
encode the same intent as the ADR, in executable form. (And remember
the SQL in service/db.py mirrors this module term for term; a deliberate
formula change touches both files.)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from service.config import (
    DEMOTION_ARCHIVED,
    DEMOTION_FORK,
    RECENCY_FLOOR,
    STARS_PIVOT_MAX,
    STARS_PIVOT_MIN,
)
from service.ranking import (
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    LaneWeights,
    ScoringWeights,
    demotion_factor,
    hybrid_score,
    is_exact_name,
    minmax_norm,
    normalise_name,
    normalise_recency,
    rrf_score,
    saturate_dependents,
    saturate_stars,
    stars_pivot,
)


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def test_rrf_rank_one_everywhere_is_the_maximum():
    lanes = LaneWeights()
    top = rrf_score({"full_text": 1, "semantic": 1, "name": 1}, lanes)
    expected = (lanes.full_text + lanes.semantic + lanes.name) / (lanes.rrf_k + 1)
    assert math.isclose(top, expected, rel_tol=1e-12)


def test_rrf_missing_lane_contributes_nothing():
    only_dense = rrf_score({"full_text": None, "semantic": 1, "name": None})
    both = rrf_score({"full_text": 1, "semantic": 1, "name": None})
    assert both > only_dense > 0.0


def test_rrf_two_mid_ranks_beat_one_top_rank():
    """The point of fusion: agreement across lanes outweighs a single
    lane's enthusiasm (at defaults, ranks 5+5 in text+dense > rank 1 in
    dense alone)."""
    agreed = rrf_score({"full_text": 5, "semantic": 5, "name": None})
    single = rrf_score({"full_text": None, "semantic": 1, "name": None})
    assert agreed > single


def test_rrf_deep_ranks_are_negligible_but_positive():
    deep = rrf_score({"full_text": 200, "semantic": None, "name": None})
    shallow = rrf_score({"full_text": 1, "semantic": None, "name": None})
    assert 0.0 < deep < shallow / 4


def test_rrf_k_flattens_rank_differences():
    """Larger k -> smaller gap between rank 1 and rank 10. That's the
    knob's purpose; pin the direction so a sweep can't invert it."""
    def gap(k: int) -> float:
        lanes = LaneWeights(rrf_k=k)
        return (rrf_score({"semantic": 1}, lanes)
                - rrf_score({"semantic": 10}, lanes))
    assert gap(20) > gap(60)


# ---------------------------------------------------------------------------
# minmax_norm
# ---------------------------------------------------------------------------

def test_minmax_norm_spans_unit_interval():
    assert minmax_norm(0.5, 0.5, 1.5) == 0.0
    assert minmax_norm(1.5, 0.5, 1.5) == 1.0
    assert minmax_norm(1.0, 0.5, 1.5) == 0.5


def test_minmax_norm_degenerate_range_is_one():
    """A single-candidate set: that candidate is the best we have."""
    assert minmax_norm(0.7, 0.7, 0.7) == 1.0


# ---------------------------------------------------------------------------
# Stars: pivot + saturation
# ---------------------------------------------------------------------------

def test_stars_pivot_is_geometric_mean():
    # geomean(100, 10_000) = 1000
    assert math.isclose(stars_pivot([100, 10_000]), 1000.0, rel_tol=1e-9)


def test_stars_pivot_clamps_to_configured_band():
    assert stars_pivot([1, 1, 1]) == STARS_PIVOT_MIN
    assert stars_pivot([10**7] * 5) == STARS_PIVOT_MAX
    assert stars_pivot([]) == STARS_PIVOT_MIN


def test_saturation_midpoint_at_pivot():
    assert saturate_stars(1000, 1000.0) == 0.5


def test_saturation_caps_rich_get_richer():
    """100K stars must not be 10x better than 10K — saturation squeezes
    the top of the range (the megastar cap from the research doc)."""
    at_10k = saturate_stars(10_000, 1000.0)
    at_100k = saturate_stars(100_000, 1000.0)
    assert at_100k < at_10k * 1.1
    assert at_100k < 1.0


def test_saturation_zero_and_negative_stars():
    assert saturate_stars(0, 1000.0) == 0.0
    assert saturate_stars(-5, 1000.0) == 0.0


def test_saturation_is_monotonic():
    values = [saturate_stars(s, 1000.0) for s in [0, 100, 1_000, 10_000, 100_000]]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# normalise_recency (floored half-life decay)
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_recency_just_pushed_is_one():
    assert math.isclose(normalise_recency(NOW, now=NOW), 1.0)


def test_recency_one_half_life_ago_is_band_midpoint():
    pushed = NOW - timedelta(days=DEFAULT_RECENCY_HALF_LIFE_DAYS)
    expected = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5
    assert math.isclose(normalise_recency(pushed, now=NOW), expected, rel_tol=1e-9)


def test_recency_never_decays_below_the_floor():
    """The bounded-maintenance property: a finished, canonical library
    from a decade ago keeps a recency floor instead of sinking to zero."""
    ancient = NOW - timedelta(days=20 * 365)
    value = normalise_recency(ancient, now=NOW)
    assert RECENCY_FLOOR < value < RECENCY_FLOOR + 0.01


def test_recency_none_is_zero():
    """No pushed_at at all scores below the floor — 'never pushed' is a
    data anomaly, not a maintenance statement."""
    assert normalise_recency(None, now=NOW) == 0.0


def test_recency_handles_naive_datetime_defensively():
    """asyncpg with TIMESTAMPTZ should always return aware datetimes,
    but a stray naive input shouldn't silently corrupt the math."""
    pushed_naive = datetime(2025, 5, 1, 12, 0, 0)  # no tzinfo
    expected = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5
    assert math.isclose(normalise_recency(pushed_naive, now=NOW), expected,
                        rel_tol=1e-3)


def test_recency_future_pushed_at_clamps_to_one():
    """Clock skew or bad data — pushed_at in the future shouldn't
    produce >1 and skew the leaderboard."""
    future = NOW + timedelta(days=10)
    assert normalise_recency(future, now=NOW) == 1.0


def test_recency_custom_half_life():
    pushed = NOW - timedelta(days=30)
    expected = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5
    assert math.isclose(
        normalise_recency(pushed, now=NOW, half_life_days=30),
        expected, rel_tol=1e-9,
    )


# ---------------------------------------------------------------------------
# Exact-name rule + demotions
# ---------------------------------------------------------------------------

def test_exact_name_matches_bare_name_and_full_name():
    assert is_exact_name("pytorch", full_name="pytorch/pytorch", name="pytorch")
    assert is_exact_name("Helm/Helm", full_name="helm/helm", name="helm")
    # Punctuation-normalised matches (ADR 0019): typing "nextjs" IS
    # typing next.js's name.
    assert is_exact_name("nextjs", full_name="vercel/next.js", name="next.js")
    assert is_exact_name("scikitlearn",
                         full_name="scikit-learn/scikit-learn",
                         name="scikit-learn")
    assert not is_exact_name("torch", full_name="pytorch/pytorch", name="pytorch")
    assert not is_exact_name("next js", full_name="vercel/next.js", name="next.js")
    assert not is_exact_name("machine learning framework",
                             full_name="pytorch/pytorch", name="pytorch")
    assert not is_exact_name("   ", full_name="a/b", name="b")


def test_demotion_factors():
    assert demotion_factor(is_archived=False, is_fork=False) == 1.0
    assert demotion_factor(is_archived=False, is_fork=True) == DEMOTION_FORK
    assert demotion_factor(is_archived=True, is_fork=False) == DEMOTION_ARCHIVED
    # Archived-and-fork is mostly archive.
    assert demotion_factor(is_archived=True, is_fork=True) == DEMOTION_ARCHIVED


# ---------------------------------------------------------------------------
# Criticality (sat(dependents), ADR 0019)
# ---------------------------------------------------------------------------

def test_saturate_dependents_none_and_zero_score_zero():
    assert saturate_dependents(None) == 0.0
    assert saturate_dependents(0) == 0.0


def test_saturate_dependents_is_half_at_pivot_and_bounded():
    assert math.isclose(saturate_dependents(1000, pivot=1000.0), 0.5)
    assert saturate_dependents(10_000_000, pivot=1000.0) < 1.0


def test_criticality_weight_defaults_dark():
    # The shipped default must add exactly nothing until the eval says
    # otherwise — this is what makes deploying the term a non-event.
    assert ScoringWeights().criticality == 0.0
    base = dict(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                stars=1000, pivot=1000.0, pushed_at=None)
    assert hybrid_score(**base) == hybrid_score(**base, dependent_count=50_000)


def test_criticality_separates_depended_on_repos_when_enabled():
    weights = ScoringWeights(criticality=0.3)
    base = dict(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                stars=1000, pivot=1000.0, pushed_at=None, weights=weights)
    library = hybrid_score(**base, dependent_count=50_000)
    app = hybrid_score(**base, dependent_count=None)
    assert library > app
    assert library - app <= 0.3  # bounded boost, never a takeover


def test_normalise_name_strips_exactly_the_sql_charset():
    # Mirrors SQL translate(lower(x), '-._', '') — spaces are NOT
    # stripped; drift between the two sides would desync exact-name
    # behaviour from the indexes in migration 0009.
    assert normalise_name(" Next.js ") == "nextjs"
    assert normalise_name("scikit-learn") == "scikitlearn"
    assert normalise_name("a_b-c.d") == "abcd"
    assert normalise_name("next js") == "next js"


# ---------------------------------------------------------------------------
# hybrid_score (the full blend)
# ---------------------------------------------------------------------------

def test_hybrid_score_combines_components_with_default_weights():
    """Spot-check the arithmetic once, end to end:
       rel_norm  = (0.02 - 0.01) / (0.03 - 0.01) = 0.5
       stars_sat = 1000 / (1000 + 1000) = 0.5
       recency   = 1.0 (pushed now)
       hybrid    = 1.0*0.5 + 0.3*0.5 + 0.2*1.0 = 0.85
    """
    s = hybrid_score(
        rrf=0.02, rrf_min=0.01, rrf_max=0.03,
        stars=1000, pivot=1000.0, pushed_at=NOW, now=NOW,
    )
    assert math.isclose(s, 0.85, rel_tol=1e-9)


def test_hybrid_score_relevance_dominates_with_default_weights():
    """The whole point of the search engine: the most relevant candidate
    at 200 stars beats the least relevant megastar. With normalised
    components, stars + recency can add at most 0.5 while relevance
    spans 1.0."""
    relevant_small = hybrid_score(
        rrf=0.03, rrf_min=0.01, rrf_max=0.03,
        stars=200, pivot=1000.0, pushed_at=NOW, now=NOW,
    )
    irrelevant_huge = hybrid_score(
        rrf=0.01, rrf_min=0.01, rrf_max=0.03,
        stars=400_000, pivot=1000.0, pushed_at=NOW, now=NOW,
    )
    assert relevant_small > irrelevant_huge


def test_hybrid_score_breaks_relevance_ties_by_stars_and_recency():
    """When fused relevance is identical, the popular and maintained
    repo wins. Stars and recency are explicitly tie-breakers."""
    a = hybrid_score(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                     stars=10_000, pivot=1000.0, pushed_at=NOW, now=NOW)
    b = hybrid_score(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                     stars=300, pivot=1000.0,
                     pushed_at=NOW - timedelta(days=5 * 365), now=NOW)
    assert a > b


def test_hybrid_score_zero_weights_disables_components():
    """Setting weights to 0 turns components off — the pure-relevance
    A/B configuration (?weights.stars=0&weights.recency=0)."""
    s = hybrid_score(
        rrf=0.02, rrf_min=0.01, rrf_max=0.03,
        stars=400_000, pivot=1000.0, pushed_at=NOW, now=NOW,
        weights=ScoringWeights(similarity=1.0, stars=0.0, recency=0.0),
    )
    assert math.isclose(s, 0.5, rel_tol=1e-9)


def test_hybrid_score_demotion_scales_the_whole_blend():
    plain = hybrid_score(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                         stars=1000, pivot=1000.0, pushed_at=NOW, now=NOW)
    fork = hybrid_score(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                        stars=1000, pivot=1000.0, pushed_at=NOW, now=NOW,
                        is_fork=True)
    assert math.isclose(fork, plain * DEMOTION_FORK, rel_tol=1e-9)


def test_hybrid_score_no_pushed_at_doesnt_break_anything():
    s = hybrid_score(rrf=0.02, rrf_min=0.01, rrf_max=0.03,
                     stars=1000, pivot=1000.0, pushed_at=None, now=NOW)
    assert math.isclose(s, 1.0 * 0.5 + 0.3 * 0.5 + 0.2 * 0.0, rel_tol=1e-9)
