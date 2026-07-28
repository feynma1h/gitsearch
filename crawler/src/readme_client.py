"""README fetching via the GitHub REST API.

The endpoint ``GET /repos/{owner}/{repo}/readme`` handles filename detection
server-side (README, README.md, README.rst, readme.txt, etc.) so we don't
have to guess. It uses GitHub's REST rate-limit budget (5000 req/hour per
token) which is *separate* from the GraphQL budget used by the metadata
crawl, so this pass does not contend with the metadata crawl.

The endpoint returns a JSON object with base64-encoded ``content``. Files
larger than 1MB return empty ``content`` and only a ``download_url``; we
fall back to fetching that URL raw. We truncate to 8KB on storage anyway,
so we only need the head of the file.

Every response (success or failure) updates the shared rate limiter via
the ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers, so the
limiter's view of remaining budget stays current across all workers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .rate_limiter import RateLimiter, parse_rest_rate_limit

logger = logging.getLogger(__name__)

# We store at most this many characters of README. Embedding models cap input
# around 8K tokens (~32KB chars); the first 8KB of a README dominates
# semantic relevance for our use case and keeps the DB lean at 100K rows.
README_MAX_CHARS = 8_192

# Maximum bytes we'll download before truncating, even from the raw download
# URL. A README claiming to be 50MB is almost certainly malformed; cap it.
README_DOWNLOAD_CAP_BYTES = 256 * 1024  # 256KB

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class ReadmeResult:
    """Outcome of a single README fetch.

    ``status`` is the value to write to ``repositories.readme_status``.
    ``content`` is set only when ``status == 'ok'``.
    """
    status: str  # 'ok', 'not_found', 'empty', 'error'
    content: Optional[str] = None
    error_detail: Optional[str] = None


class ReadmeClient:
    """Async client for fetching one repo's README via REST."""

    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._token = token
        self._limiter = limiter

    async def fetch(self, owner: str, repo: str) -> ReadmeResult:
        """Fetch the README for one repo. Never raises on per-repo failures.

        Returns a :class:`ReadmeResult` with the status to record. Transient
        errors (network, 5xx) and rate limits (403/429) are retried
        internally; if non-rate-limit retries are exhausted the result is
        ``status='error'`` so the caller can move on.
        """
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/readme"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-crawler/1.0",
        }

        last_error: Optional[str] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    await self._update_limiter(resp.headers)

                    if resp.status == 404:
                        return ReadmeResult(status="not_found")

                    if resp.status == 451:
                        # DMCA / legal takedown. Never retry.
                        return ReadmeResult(
                            status="not_found",
                            error_detail="451 unavailable for legal reasons",
                        )

                    if resp.status in (403, 429) and await self._is_rate_limit(resp):
                        # Not a per-repo failure — don't record readme_fetched_at.
                        last_error = f"HTTP {resp.status}: rate limited"
                        continue

                    if resp.status in _RETRY_STATUSES:
                        last_error = f"HTTP {resp.status}"
                        await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))
                        continue

                    if resp.status != 200:
                        body = await resp.text()
                        return ReadmeResult(
                            status="error",
                            error_detail=f"HTTP {resp.status}: {body[:200]}",
                        )

                    payload = await resp.json()
                    return await self._decode_payload(payload, headers)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** attempt))

        return ReadmeResult(
            status="error",
            error_detail=f"exhausted retries; last error: {last_error}",
        )

    async def _update_limiter(self, headers) -> None:
        parsed = parse_rest_rate_limit(headers)
        if parsed is not None:
            remaining, reset_at = parsed
            await self._limiter.update(remaining, reset_at)

    async def _is_rate_limit(self, resp: aiohttp.ClientResponse) -> bool:
        """Detect a rate-limit 403/429 and wait out its reset in place.

        GitHub signals two distinct kinds: a secondary/abuse limit, which
        carries an explicit ``Retry-After`` header, and the primary hourly
        quota, exposed as ``X-RateLimit-Remaining: 0`` (already recorded on
        the shared limiter by ``_update_limiter`` above). Returns True if
        this was a rate limit — the caller should retry the same request —
        or False for a genuine per-repo 403 (e.g. permission denied).
        """
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                wait_s = float(retry_after)
            except ValueError:
                wait_s = 60.0
            await asyncio.sleep(wait_s + 1.0)
            return True

        parsed = parse_rest_rate_limit(resp.headers)
        if parsed is not None and parsed[0] == 0:
            await self._limiter.wait_if_needed()
            return True

        return False

    async def _decode_payload(
        self,
        payload: dict,
        headers: dict,
    ) -> ReadmeResult:
        encoded = payload.get("content", "")
        encoding = payload.get("encoding", "")

        # Files > 1MB: content is empty, must fetch raw via download_url.
        if not encoded and payload.get("download_url"):
            return await self._fetch_raw(payload["download_url"], headers)

        if encoding != "base64":
            return ReadmeResult(
                status="error",
                error_detail=f"unexpected encoding: {encoding!r}",
            )

        try:
            raw = base64.b64decode(encoded)
        except Exception as exc:  # noqa: BLE001 — narrow surface, broad catch ok
            return ReadmeResult(
                status="error",
                error_detail=f"base64 decode failed: {exc}",
            )

        return _finalize(raw)

    async def _fetch_raw(self, url: str, headers: dict) -> ReadmeResult:
        """Stream a large README via its download_url, capping bytes read."""
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return ReadmeResult(
                        status="error",
                        error_detail=f"raw fetch HTTP {resp.status}",
                    )
                raw = await resp.content.read(README_DOWNLOAD_CAP_BYTES)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return ReadmeResult(
                status="error",
                error_detail=f"raw fetch error: {exc}",
            )

        return _finalize(raw)


def _finalize(raw: bytes) -> ReadmeResult:
    """Decode bytes to UTF-8, classify empty, and truncate to the storage cap."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ReadmeResult(status="empty")
    if len(text) > README_MAX_CHARS:
        text = text[:README_MAX_CHARS]
    return ReadmeResult(status="ok", content=text)
