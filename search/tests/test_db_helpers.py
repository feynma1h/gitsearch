"""Tests for the pure helper functions in service/db.py — the pieces
of the retrieval SQL's input preparation that Python owns: coverage
slot construction, the fuzzy-arm gate, LIKE-pattern escaping, and the
two-variant SQL builder (ADR 0020).
"""

from __future__ import annotations

import re

from service.config import FTS_COVERAGE_SLOTS, NAME_FUZZY_MAX_TOKENS
from service.db import (
    _build_search_sql,
    coverage_slots,
    fuzzy_name_query,
    like_prefix_pattern,
    name_query,
    normalized_name_query,
)


def test_coverage_slots_quoted_and_padded() -> None:
    slots = coverage_slots(["machin", "learn"])
    assert slots[0] == "'machin'"
    assert slots[1] == "'learn'"
    assert slots[2:] == [""] * (FTS_COVERAGE_SLOTS - 2)
    assert len(slots) == FTS_COVERAGE_SLOTS


def test_coverage_slots_count_any_field() -> None:
    # No weight labels: coverage counts a term in ANY light field. A
    # topics-only (:B) restriction was reverted — see coverage_slots()
    # — so a reappearing ':B' here is a regression, not a cleanup.
    assert all(":B" not in s for s in coverage_slots(["machin", "learn"]))


def test_coverage_slots_truncate_long_queries() -> None:
    lexemes = [f"lex{i}" for i in range(FTS_COVERAGE_SLOTS + 3)]
    slots = coverage_slots(lexemes)
    assert len(slots) == FTS_COVERAGE_SLOTS
    assert slots[-1] == f"'lex{FTS_COVERAGE_SLOTS - 1}'"


def test_coverage_slots_strip_quotes() -> None:
    # A lexeme can't legitimately contain a quote, but the slot text is
    # spliced into tsquery literals — never allow one through.
    assert coverage_slots(["o'brien"])[0] == "'obrien'"


def test_fuzzy_gate_allows_short_queries_only() -> None:
    assert fuzzy_name_query("pytorhc") == "pytorhc"
    assert fuzzy_name_query("Tailwind CSS") == "tailwind css"
    assert fuzzy_name_query("react state management zustand") == ""
    assert fuzzy_name_query("   ") == ""
    # Boundary: exactly the max token count passes.
    assert len("a b".split()) <= NAME_FUZZY_MAX_TOKENS
    assert fuzzy_name_query("a b") == "a b"


def test_like_prefix_pattern_escapes_metacharacters() -> None:
    assert like_prefix_pattern("FastAPI") == "fastapi%"
    assert like_prefix_pattern("50%_off\\x") == "50\\%\\_off\\\\x%"
    assert like_prefix_pattern("  ") == ""


def test_name_query_normalises() -> None:
    assert name_query("  PyTorch ") == "pytorch"


def test_normalized_name_query_strips_punctuation_only_for_names() -> None:
    assert normalized_name_query("Next.js") == "nextjs"
    assert normalized_name_query("scikit-learn") == "scikitlearn"
    assert normalized_name_query("vercel/next.js") == "vercel/nextjs"
    # Multi-word queries and empty/punctuation-only queries disable the
    # arm with None (NULL never compares true) rather than "".
    assert normalized_name_query("machine learning framework") is None
    assert normalized_name_query("...") is None
    assert normalized_name_query("  ") is None


def _param_numbers(sql: str) -> set[int]:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


def test_both_sql_variants_bind_the_same_34_parameters() -> None:
    # search() always sends 34 arguments; a prepared statement infers
    # its parameter list from the query text, so BOTH variants must
    # reference every slot ($32/$33 via the phase-1 params anchor).
    expected = set(range(1, 35))
    assert _param_numbers(_build_search_sql(True)) == expected
    assert _param_numbers(_build_search_sql(False)) == expected


def test_enrichment_coverage_is_capped_at_one_term() -> None:
    # Enrichment may COMPLETE a coverage tier, never build one: the
    # phase-2 expression wraps enrichment-only terms in LEAST(1, ...).
    # Removing the cap re-opens the megastar tier-takeover measured in
    # the first phase-2 eval (bootstrap winning "game engine").
    on, off = _build_search_sql(True), _build_search_sql(False)
    assert "LEAST(1," in on
    assert "LEAST(1," not in off


def test_phase_flag_controls_enrichment_references() -> None:
    on, off = _build_search_sql(True), _build_search_sql(False)
    for table in ("repository_enrichment", "repository_signals"):
        assert table in on
        assert table not in off
    assert "translate(lower(" in on
    assert "translate(lower(" not in off
    # Phase 1 keeps the response shape: criticality still computed,
    # from a constant-NULL dependent count.
    assert "criticality_contribution" in off
    assert "NULL::int AS dependent_count" in off
