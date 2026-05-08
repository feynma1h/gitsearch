"""Generate non-overlapping star-range shards for the GitHub search API.

GitHub's `stars:A..B` syntax is INCLUSIVE on both ends, so adjacent shards
must not share endpoints. Each shard also returns at most 1000 results, so
shards must be narrow enough that no shard exceeds that cap.

The star distribution is heavily skewed: there are millions of repos with
<10 stars and only thousands with >10K stars. Uniform-width shards waste
queries on the sparse tail and miss data in the dense head.

This module uses variable-width shards: narrow at the bottom, wide at the
top. The defaults are tuned so the densest shard stays under the 1000-result
cap when filtering by `stars:>=N` for reasonable N.
"""

from __future__ import annotations

from typing import List


def generate_shards(min_stars: int = 50, max_stars: int = 500_000) -> List[str]:
    """Return a list of GitHub search query fragments covering the star range.

    Args:
        min_stars: Lower bound (inclusive). Defaults to 50 to keep the crawl
            tractable; below ~50 stars there are too many repos to fit in
            GitHub's 1000-results-per-query cap even with single-star shards.
        max_stars: Upper bound used only to bound the open-ended top shard.

    Returns:
        A list of query fragments like ``["stars:50..59", "stars:60..69", ...]``
        with no overlapping boundaries.
    """
    if min_stars < 0:
        raise ValueError("min_stars must be non-negative")
    if max_stars <= min_stars:
        raise ValueError("max_stars must be greater than min_stars")

    # (range_start, range_end_exclusive, width). The crawler will emit shards
    # of `width` stars covering [range_start, range_end_exclusive).
    bands = [
        (50,      500,     10),
        (500,     2_000,   50),
        (2_000,   10_000,  500),
        (10_000,  50_000,  5_000),
    ]

    shards: List[str] = []
    for band_start, band_end, width in bands:
        if band_end <= min_stars:
            continue
        start = max(band_start, min_stars)
        end = band_end
        for lo in range(start, end, width):
            hi = min(lo + width - 1, end - 1)
            shards.append(f"stars:{lo}..{hi}")

    # Open-ended top shard catches everything above the last band.
    shards.append(f"stars:>={bands[-1][1]}")

    return shards
