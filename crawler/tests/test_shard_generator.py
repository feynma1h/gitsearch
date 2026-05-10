"""Tests for the shard generator.

The boundary semantics are subtle and easy to break, so these tests pin
down: no overlaps, no gaps, every shard is well-formed, the open-ended
top shard chains with the last range shard, and the density-calibrated
band layout has not silently regressed.

The band-count tests in particular guard against the structural
undersizing bug described in ADR 0001: a wide low-star band (e.g. a
single 200..209 shard) is clipped at GitHub's 1000-result cap and
silently loses ~90% of the repos in that range.
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


def _range_shards(shards):
    return [_parse(s) for s in shards if _parse(s)[2] == "range"]


def test_default_shards_are_non_empty():
    assert len(generate_shards()) > 0


def test_every_shard_is_well_formed():
    """Each range shard must have lo <= hi; each open shard must parse."""
    for s in generate_shards():
        lo, hi, kind = _parse(s)
        if kind == "range":
            assert lo <= hi, f"inverted range in {s}"
        else:
            assert lo > 0


def test_no_boundary_overlaps_or_gaps():
    range_shards = _range_shards(generate_shards())
    for (lo, hi, _), (next_lo, _, _) in zip(range_shards, range_shards[1:]):
        assert hi < next_lo, f"overlap between {lo}..{hi} and {next_lo}.."
        assert next_lo == hi + 1, f"gap between {lo}..{hi} and {next_lo}.."


def test_open_ended_top_shard_chains_with_last_range():
    """The open-ended top shard must start exactly one above the last range
    shard's hi — otherwise we silently skip a star value at the seam."""
    shards = generate_shards()
    assert shards[-1].startswith("stars:>=")
    _, last_range_hi, _ = _parse(shards[-2])
    open_lo, _, _ = _parse(shards[-1])
    assert open_lo == last_range_hi + 1, (
        f"seam gap: last range ends at {last_range_hi}, open starts at {open_lo}"
    )


def test_min_stars_is_respected():
    shards = generate_shards(min_stars=1000)
    first_lo, _, _ = _parse(shards[0])
    assert first_lo >= 1000


def test_min_stars_above_all_bands_still_emits_open_shard():
    """If the caller asks for a min_stars above every band, we should still
    emit something — at minimum the open-ended top shard."""
    shards = generate_shards(min_stars=200_000)
    assert len(shards) >= 1
    assert shards[-1].startswith("stars:>=")


def test_invalid_args_raise():
    with pytest.raises(ValueError):
        generate_shards(min_stars=-1)
    with pytest.raises(ValueError):
        generate_shards(min_stars=100, max_stars=50)


# ---------------------------------------------------------------------------
# Density-calibration regression tests.
#
# The widths below are tuned to keep every shard under GitHub's 1000-result
# cap given the actual repo density at each star range (see ADR 0001).
# Widening any of these bands risks silently clipping the densest shard in
# the band; if you're tempted to widen one, read the ADR first.
# ---------------------------------------------------------------------------

_EXPECTED_BANDS = [
    # (band_lo_inclusive, band_hi_exclusive, expected_shard_count)
    (200,    400,    200),  # 1-star  shards
    (400,    1_000,  300),  # 2-star  shards
    (1_000,  2_000,  20),   # 50-star shards
    (2_000,  5_000,  30),   # 100-star shards
    (5_000,  10_000, 10),   # 500-star shards
    (10_000, 50_000, 8),    # 5000-star shards
]


def test_band_shard_counts_match_expected():
    """Lock in the density-calibrated layout. If GitHub's distribution
    shifts enough that we need to recalibrate, update _EXPECTED_BANDS *and*
    add a consequence to ADR 0001 explaining why."""
    shards = generate_shards()
    range_shards = _range_shards(shards)
    for band_lo, band_hi, expected_count in _EXPECTED_BANDS:
        actual = sum(1 for lo, _, _ in range_shards if band_lo <= lo < band_hi)
        assert actual == expected_count, (
            f"band [{band_lo}, {band_hi}): expected {expected_count} shards, got {actual}"
        )


def test_low_star_bands_use_narrow_widths():
    """The 200-399 band must use 1-star widths and the 400-999 band must
    use 2-star widths. Anything wider risks clipping at the 1000-result
    cap given current repo density at the low-star end."""
    shards = generate_shards()
    range_shards = _range_shards(shards)

    for lo, hi, _ in range_shards:
        if 200 <= lo < 400:
            assert hi - lo == 0, f"shard {lo}..{hi} in 200-399 band must be 1-star wide"
        elif 400 <= lo < 1_000:
            assert hi - lo == 1, f"shard {lo}..{hi} in 400-999 band must be 2-star wide"


def test_total_shard_count_is_in_expected_range():
    """A blunt sanity check that total shard count is what we planned for
    (~568 range shards + 1 open). Catches accidental band-table edits."""
    shards = generate_shards()
    assert 565 <= len(shards) <= 575, f"got {len(shards)} shards, expected ~569"
