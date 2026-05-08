"""Tests for the rate limiter and response parsers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.rate_limiter import (
    RateLimiter,
    parse_graphql_rate_limit,
    parse_rest_rate_limit,
)


@pytest.mark.asyncio
async def test_does_not_block_when_budget_is_high():
    limiter = RateLimiter()
    await asyncio.wait_for(limiter.wait_if_needed(), timeout=0.1)


@pytest.mark.asyncio
async def test_update_records_remaining_and_reset():
    limiter = RateLimiter()
    reset = datetime.now(timezone.utc) + timedelta(minutes=5)
    await limiter.update(remaining=42, reset_at=reset)
    assert limiter.remaining == 42


@pytest.mark.asyncio
async def test_blocks_briefly_when_no_reset_seen_yet():
    """If the budget is low but no reset_at is known, fall back to a short sleep."""
    limiter = RateLimiter()
    limiter._remaining = 0  # type: ignore[attr-defined]
    start = asyncio.get_event_loop().time()
    await limiter.wait_if_needed()
    elapsed = asyncio.get_event_loop().time() - start
    assert 0.5 < elapsed < 2.0


@pytest.mark.asyncio
async def test_concurrent_updates_do_not_race():
    """Many workers updating concurrently must end at one of the input values."""
    limiter = RateLimiter()
    reset = datetime.now(timezone.utc) + timedelta(minutes=1)

    async def updater(value: int):
        await limiter.update(remaining=value, reset_at=reset)

    await asyncio.gather(*(updater(i) for i in range(100)))
    assert 0 <= limiter.remaining < 100


def test_parse_graphql_rate_limit():
    payload = {"remaining": 4321, "resetAt": "2026-01-01T12:00:00Z", "cost": 1}
    remaining, reset_at = parse_graphql_rate_limit(payload)
    assert remaining == 4321
    assert reset_at.tzinfo is not None
    assert reset_at.year == 2026


def test_parse_rest_rate_limit_with_headers():
    headers = {"X-RateLimit-Remaining": "100", "X-RateLimit-Reset": "1767182400"}
    parsed = parse_rest_rate_limit(headers)
    assert parsed is not None
    remaining, reset_at = parsed
    assert remaining == 100
    assert reset_at.tzinfo is not None


def test_parse_rest_rate_limit_missing_headers():
    assert parse_rest_rate_limit({}) is None
    assert parse_rest_rate_limit({"X-RateLimit-Remaining": "5"}) is None
