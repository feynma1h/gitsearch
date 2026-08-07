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

from datetime import datetime
from typing import List


def generate_shards(min_stars: int = 200) -> List[str]:
    """Return a list of GitHub search query fragments covering the star range.

    Args:
        min_stars: Lower bound (inclusive). Defaults to 200 to keep the crawl
            tractable; below ~200 stars there are too many repos to fit in
            GitHub's 1000-results-per-query cap even with single-star shards.

    Returns:
        A list of query fragments like ``["stars:200..200", "stars:201..201", ...]``
        with no overlapping boundaries.
    """
    if min_stars < 200:
        raise ValueError(
            "min_stars must be >= 200; lower ranges exceed GitHub's "
            "1000-results-per-query cap"
        )

    # (range_start, range_end_exclusive, width). The crawler will emit shards
    # of `width` stars covering [range_start, range_end_exclusive).
    bands = [
        (200,     400,     1),
        (400,     1_000,   2),
        (1_000,   2_000,   50),
        (2_000,   5_000,   100),
        (5_000,   10_000,  500),
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


def apply_pushed_since(shards: List[str], since: datetime) -> List[str]:
    """Narrow each shard query to repos pushed on or after ``since``.

    Appends GitHub's ``pushed:>=YYYY-MM-DD`` qualifier — ANDed with the
    existing ``stars:A..B`` fragment — so an incremental crawl only pulls
    repos with recent commits instead of re-scanning the whole corpus.

    Day granularity is deliberate: it re-includes anything pushed on the
    watermark date, a harmless overlap since ``db.insert_batch`` upserts
    by id. See ADR 0015 for why ``pushed:`` (not ``created:``) is the
    signal and how star-count drift is handled separately.
    """
    date = since.date().isoformat()
    return [f"{shard} pushed:>={date}" for shard in shards]
