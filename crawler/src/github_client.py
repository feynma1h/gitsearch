"""GraphQL client for the GitHub Search API.

Wraps a single endpoint (``search(type: REPOSITORY)``) with retry-on-transient
behaviour. All other concerns (rate limiting, queueing, persistence) live in
sibling modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import aiohttp

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# Fields chosen for downstream semantic search:
#   - `nameWithOwner` lets us reconstruct the repo URL and is the canonical
#     human-readable identifier.
#   - `primaryLanguage` and `repositoryTopics` are key filtering signals.
#   - `pushedAt` is needed for recency-based re-ranking.
#   - `isArchived` and `isFork` let downstream stages drop dead/derivative work.
SEARCH_QUERY = """
query ($q: String!, $cursor: String) {
  search(query: $q, type: REPOSITORY, first: 100, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    repositoryCount
    nodes {
      ... on Repository {
        id
        name
        nameWithOwner
        owner { login }
        description
        url
        homepageUrl
        primaryLanguage { name }
        repositoryTopics(first: 20) {
          nodes { topic { name } }
        }
        stargazerCount
        forkCount
        isArchived
        isFork
        createdAt
        pushedAt
      }
    }
  }
  rateLimit {
    remaining
    resetAt
    cost
  }
}
"""

# Errors we retry. Anything else (auth failures, malformed queries) is fatal.
_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF_SECONDS = 1.0

# Secondary rate limit handling.
#
# GitHub's *secondary* rate limit is a separate, mostly-undocumented
# throttle layered on top of the primary 5000 pts/hour budget. It
# triggers on patterns GitHub considers abusive (concurrency, burst
# rate, etc.) and surfaces only as HTTP 403 with a body containing
# "secondary rate limit". Unlike the primary budget, it's not visible
# in any response we can pre-empt against.
#
# When detected we (a) sleep for a long time, (b) tell the shared
# RateLimiter to globally pause so other workers don't blow past us,
# and (c) retry the request a few times. If GitHub keeps refusing
# after _SECONDARY_MAX_RETRIES backoffs, something more serious is
# wrong; we surface it as a regular GitHubAPIError.
_SECONDARY_RATE_LIMIT_MARKER = "secondary rate limit"
_SECONDARY_BASE_SLEEP_SECONDS = 60.0
_SECONDARY_MAX_RETRIES = 3


class GitHubAPIError(Exception):
    """Non-retryable GitHub API error (auth, query, schema, etc.)."""


def _parse_retry_after(headers: aiohttp.typedefs.LooseHeaders) -> Optional[float]:
    """Read GitHub's Retry-After header if present. Returns seconds or None.

    GitHub sends Retry-After on some (not all) secondary-rate-limit
    responses. When it's there it's authoritative; when it's not we
    fall back to a base sleep that doubles per attempt.
    """
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def fetch_repos(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    cursor: Optional[str] = None,
    limiter: Optional[RateLimiter] = None,
) -> Dict[str, Any]:
    """Execute one page of a search and return the parsed ``data`` block.

    Retries on transient HTTP errors with exponential backoff. Detects
    GitHub's secondary rate limit and triggers a global pause via
    ``limiter`` so concurrent workers also back off. Raises
    :class:`GitHubAPIError` on any non-retryable error.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "github-crawler/1.0",
    }
    payload = {
        "query": SEARCH_QUERY,
        "variables": {"q": query, "cursor": cursor},
    }

    last_exc: Optional[Exception] = None
    secondary_attempts = 0
    for attempt in range(_MAX_RETRIES):
        try:
            async with session.post(
                GITHUB_GRAPHQL_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in _RETRY_STATUSES:
                    body = await resp.text()
                    logger.warning(
                        "Transient %d from GitHub (attempt %d/%d): %s",
                        resp.status, attempt + 1, _MAX_RETRIES, body[:200],
                    )
                    await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))
                    continue

                if resp.status == 403:
                    body = await resp.text()
                    if _SECONDARY_RATE_LIMIT_MARKER in body.lower():
                        if secondary_attempts >= _SECONDARY_MAX_RETRIES:
                            raise GitHubAPIError(
                                f"Secondary rate limit not clearing after "
                                f"{_SECONDARY_MAX_RETRIES} backoffs; aborting."
                            )
                        # Honor Retry-After if GitHub sent one; otherwise
                        # exponential backoff from a 60s base.
                        retry_after = _parse_retry_after(resp.headers)
                        sleep_seconds = retry_after if retry_after is not None \
                            else _SECONDARY_BASE_SLEEP_SECONDS * (2 ** secondary_attempts)
                        logger.warning(
                            "Secondary rate limit hit (attempt %d/%d). "
                            "Sleeping %.0fs and globally pausing other workers.",
                            secondary_attempts + 1, _SECONDARY_MAX_RETRIES,
                            sleep_seconds,
                        )
                        if limiter is not None:
                            await limiter.pause(sleep_seconds)
                        await asyncio.sleep(sleep_seconds)
                        secondary_attempts += 1
                        continue
                    # Non-secondary 403 (auth, permissions, etc.) is fatal.
                    raise GitHubAPIError(f"GitHub returned 403: {body[:500]}")

                if resp.status != 200:
                    body = await resp.text()
                    raise GitHubAPIError(
                        f"GitHub returned {resp.status}: {body[:500]}"
                    )

                data = await resp.json()

            if "errors" in data:
                # GitHub returns 200 + errors[] for query-level problems.
                # These are not retryable.
                raise GitHubAPIError(f"GraphQL errors: {data['errors']}")

            return data["data"]

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "Network error on attempt %d/%d: %s",
                attempt + 1, _MAX_RETRIES, exc,
            )
            await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))

    raise GitHubAPIError(
        f"Exhausted {_MAX_RETRIES} retries; last error: {last_exc}"
    )
