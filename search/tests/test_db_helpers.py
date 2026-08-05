"""Tests for the pure helper functions in service/db.py — the pieces
of the retrieval SQL's input preparation that Python owns: coverage
slot construction, the fuzzy-arm gate, and LIKE-pattern escaping.
"""

from __future__ import annotations

from service.config import FTS_COVERAGE_SLOTS, NAME_FUZZY_MAX_TOKENS
from service.db import (
    coverage_slots,
    fuzzy_name_query,
    like_prefix_pattern,
    name_query,
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
