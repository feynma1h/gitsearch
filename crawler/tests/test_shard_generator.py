"""Tests for the shard generator.

The boundary semantics are subtle and easy to break, so these tests pin
down: no overlaps, no gaps, every shard is well-formed, and the open-ended
top shard is present.
"""

from __future__ import annotations

import re

import pytest

from src.shard_generator import generate_shards

_RANGE_RE = re.compile(r"^stars:(\d+)\.\.(\d+)$")
_OPEN_RE = re.compile(r"^stars:>=(\d+)$")


def _parse(shard: str):
    if m := _RANGE_RE.match(shard):
        return int(m.group(1)), int(m.group(2)), "range"
    if m := _OPEN_RE.match(shard):
        return int(m.group(1)), None, "open"
    raise AssertionError(f"Malformed shard: {shard}")


def test_default_shards_are_non_empty():
    assert len(generate_shards()) > 0


def test_no_boundary_overlaps_or_gaps():
    shards = generate_shards()
    range_shards = [_parse(s) for s in shards if _parse(s)[2] == "range"]
    for (lo, hi, _), (next_lo, _, _) in zip(range_shards, range_shards[1:]):
        assert hi < next_lo, f"overlap between {lo}..{hi} and {next_lo}.."
        assert next_lo == hi + 1, f"gap between {lo}..{hi} and {next_lo}.."


def test_open_ended_top_shard_exists():
    shards = generate_shards()
    assert any(s.startswith("stars:>=") for s in shards)
    # And it should be the last shard.
    assert shards[-1].startswith("stars:>=")


def test_min_stars_is_respected():
    shards = generate_shards(min_stars=1000)
    first_lo, _, _ = _parse(shards[0])
    assert first_lo >= 1000


def test_invalid_args_raise():
    with pytest.raises(ValueError):
        generate_shards(min_stars=-1)
    with pytest.raises(ValueError):
        generate_shards(min_stars=100, max_stars=50)
