"""Generic rate limiter for the GitHub APIs.

Both the GraphQL and REST APIs report rate-limit state on every response,
just in different shapes (response body vs. headers, ISO string vs. Unix
timestamp). This limiter is shape-agnostic: callers parse their response
into ``(remaining, reset_at)`` and pass that to :meth:`update`.

Concurrent workers share a single limiter instance, so all reads and writes
of internal state are guarded by an asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class RateLimiter:
    """Coordinates request pacing across concurrent workers.

    The limiter holds a best-known remaining budget and reset timestamp,
    both supplied by the caller after every response. When the budget
    drops below :attr:`LOW_WATER_MARK`, :meth:`wait_if_needed` sleeps
    until the reported reset time.
    """

    # Stop issuing requests when remaining budget falls below this threshold.
    #
    # This is the safety margin against the read-fire-update race: many workers
    # can read `remaining` before any of them has issued a request and called
    # update(). If `num_workers * max_cost_per_query` exceeds this margin, the
    # crawler can briefly overshoot and trigger 403s. With 15 workers and 1 pt
    # per search query, an overshoot of 15 is the worst case, so 50 is safe.
    LOW_WATER_MARK = 50

    def __init__(self, name: str = "github", initial_budget: int = 5000) -> None:
        self._name = name
        self._remaining: int = initial_budget
        self._reset_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

    async def wait_if_needed(self) -> None:
        """Block until it is safe to issue another request."""
        async with self._lock:
            if self._remaining > self.LOW_WATER_MARK:
                return

            if self._reset_at is None:
                # No info yet; back off briefly and let the first response land.
                await asyncio.sleep(1.0)
                return

            now = datetime.now(timezone.utc)
            sleep_seconds = (self._reset_at - now).total_seconds()
            if sleep_seconds > 0:
                logger.warning(
                    "[%s] Rate limit nearly exhausted (remaining=%d). "
                    "Sleeping %.1fs until reset.",
                    self._name, self._remaining, sleep_seconds,
                )
                await asyncio.sleep(sleep_seconds + 1.0)  # +1s safety margin

    async def update(self, remaining: int, reset_at: datetime) -> None:
        """Record the latest rate-limit state.

        Args:
            remaining: Budget remaining as reported by GitHub.
            reset_at: Timezone-aware datetime when the budget resets.
        """
        async with self._lock:
            self._remaining = remaining
            self._reset_at = reset_at

    @property
    def remaining(self) -> int:
        """Current best-known remaining budget (informational only)."""
        return self._remaining


# Helpers for parsing the two GitHub response shapes into (remaining, reset_at).

def parse_graphql_rate_limit(rate_limit: dict) -> Tuple[int, datetime]:
    """Parse the GraphQL ``rateLimit`` block from a response body."""
    return (
        rate_limit["remaining"],
        datetime.fromisoformat(rate_limit["resetAt"].replace("Z", "+00:00")),
    )


def parse_rest_rate_limit(headers) -> Optional[Tuple[int, datetime]]:
    """Parse REST rate-limit headers. Returns ``None`` if headers are missing.

    The REST API sets ``X-RateLimit-Remaining`` (int) and
    ``X-RateLimit-Reset`` (Unix timestamp) on every response.
    """
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining is None or reset is None:
        return None
    return (
        int(remaining),
        datetime.fromtimestamp(int(reset), tz=timezone.utc),
    )
