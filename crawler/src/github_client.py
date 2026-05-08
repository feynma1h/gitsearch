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


class GitHubAPIError(Exception):
    """Non-retryable GitHub API error (auth, query, schema, etc.)."""


async def fetch_repos(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute one page of a search and return the parsed ``data`` block.

    Retries on transient HTTP errors with exponential backoff. Raises
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
