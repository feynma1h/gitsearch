"""deps.dev signal ingestion: dependent counts + OpenSSF Scorecard.

Usage:
    DATABASE_URL=postgresql://... \\
        python -m src.deps_dev_pass [--top-n 30000] [--scorecard-all]

Fills ``repository_signals`` (migration 0010) from Google's deps.dev
API — free, keyless, CC-BY, keyed exactly as our corpus
(github.com/owner/repo). Two sub-passes:

  1. **Scorecard batch** (cheap): POST /v3alpha/projectbatch, 5,000
     project keys per call, ~54 calls for the whole corpus. Records
     scorecard overall score + date for every repo deps.dev knows.
  2. **Dependents** (per-repo API walk, so scoped to --top-n by
     stars): for each repo, map project -> published packages
     (:packageversions, SOURCE_REPO relation only), pick up to
     MAX_PACKAGES_PER_REPO plausible packages (name-similarity first —
     a monorepo lists hundreds), resolve each package's default
     version, and read that version's dependentCount. The repo's
     signal is the MAX across its packages ("the flagship package"),
     recorded with which package carried it.

Semantics note, recorded once (probed live 2026-08-06): dependentCount
is per *version*, attributed by deps.dev's resolution snapshot, and it
is NOT comparable across ecosystems — PYPI defaults look sane
(torch@2.13.0: 24,273), NPM fragments across version lines
(react@19.2.4: 778 for a package with ~200K true dependents;
@18.2.0: 13,437), and CARGO returned 0 for every recent serde version.
We therefore take the MAX over a sampled set of versions (default +
newest few + evenly spaced older) per package, store it as a lower
bound, and the ranking weight that consumes this signal ships at 0.0
until the eval proves it helps (ADR 0019). If the signal earns its
weight, the upgrade path is OpenSSF criticality_score's published
dataset, which aggregates deps.dev dependents properly.

The pass is resumable and idempotent: rows upsert by repo_id, and
--max-age-days skips repos refreshed recently. Dependents are only
attempted for repos the scorecard pass confirmed deps.dev knows —
which also spares the API 404 traffic.

Politeness: unauthenticated API; concurrency stays low (default 8),
retries honour Retry-After, and the client sends a descriptive
User-Agent, per their guidance.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import aiohttp
import asyncpg

from .db import create_pool

logger = logging.getLogger(__name__)

API_BASE = "https://api.deps.dev/v3alpha"
USER_AGENT = "gitsearch-signals/1.0 (repository search engine; batch ingest)"

PROJECT_BATCH_SIZE = 5_000       # documented limit of /projectbatch
MAX_PACKAGES_PER_REPO = 3        # monorepos publish hundreds; walk the head
MAX_VERSION_SAMPLES = 6          # versions probed per package (see above)
DEFAULT_WORKERS = 8
# Dependents walking costs up to ~20 calls/repo; the top 3K by stars
# covers essentially every repo that can reach a top-10, which is the
# only place the criticality term can matter.
DEFAULT_TOP_N = 3_000

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub("", text.lower())


# ---------------------------------------------------------------------------
# Pure selection logic (unit-tested)
# ---------------------------------------------------------------------------


def select_packages(
    versions: Sequence[dict], repo_name: str,
    cap: int = MAX_PACKAGES_PER_REPO,
) -> List[Tuple[str, str]]:
    """From a :packageversions response, choose the (system, name)
    pairs worth querying for dependents.

    Only SOURCE_REPO relations count (ISSUE_TRACKER links are not
    "this repo is this package"). Ranking: packages whose normalised
    name matches the repo name first (the flagship is almost always
    eponymous — "torch" for pytorch/pytorch), then shorter names (core
    packages before plugin-of-plugin), alphabetical as the tiebreak so
    runs are deterministic.
    """
    seen: Dict[Tuple[str, str], None] = {}
    for entry in versions:
        if entry.get("relationType") != "SOURCE_REPO":
            continue
        key = entry.get("versionKey") or {}
        system, name = key.get("system"), key.get("name")
        if system and name:
            seen.setdefault((system, name), None)

    rn = _norm(repo_name)

    def rank(pair: Tuple[str, str]) -> Tuple:
        _system, name = pair
        n = _norm(name.rsplit("/", 1)[-1].rsplit(":", 1)[-1])
        eponymous = n == rn or n.endswith(rn) or rn.endswith(n) if n else False
        return (not eponymous, len(name), pair)

    return sorted(seen, key=rank)[:cap]


def pick_default_version(package: dict) -> Optional[str]:
    """The package's default version, per deps.dev's own flag."""
    for version in package.get("versions", []):
        if version.get("isDefault"):
            key = version.get("versionKey") or {}
            return key.get("version")
    return None


def sample_versions(package: dict) -> List[str]:
    """Versions to probe for dependents: default + newest few + evenly
    spaced older ones, MAX_VERSION_SAMPLES total.

    The spread exists because the resolution snapshot parks each
    ecosystem's dependent mass on different version vintages (see the
    module docstring); newest-biased sampling catches caret-style
    ecosystems, the evenly spaced tail catches long-lived pins.
    """
    versions = package.get("versions", [])
    ordered = sorted(
        versions,
        key=lambda v: v.get("publishedAt") or "",
        reverse=True,
    )
    names = [
        (v.get("versionKey") or {}).get("version")
        for v in ordered
    ]
    names = [n for n in names if n]

    picks: List[str] = []
    default = pick_default_version(package)
    if default:
        picks.append(default)
    n = len(names)
    for idx in (0, 1, 2, n // 4, n // 2, (3 * n) // 4):
        if idx < n and names[idx] not in picks:
            picks.append(names[idx])
        if len(picks) >= MAX_VERSION_SAMPLES:
            break
    return picks[:MAX_VERSION_SAMPLES]


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class DepsDevClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _request(
        self, method: str, url: str, payload: Optional[dict] = None,
    ) -> Optional[dict]:
        """One API call with retries. Returns None for 404 (unknown
        project/package — a data fact, not an error)."""
        last = "no attempt"
        for attempt in range(_MAX_RETRIES):
            try:
                async with self._session.request(
                    method, url, json=payload,
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status in _RETRY_STATUSES:
                        retry_after = resp.headers.get("Retry-After")
                        wait = (
                            float(retry_after) if retry_after
                            else _BASE_BACKOFF * (2 ** attempt)
                        )
                        last = f"HTTP {resp.status}"
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
        raise RuntimeError(f"deps.dev request failed: {url}: {last}")

    async def project_batch(self, project_ids: Sequence[str]) -> Dict[str, dict]:
        """Resolve up to 5,000 project keys -> project objects.

        Follows nextPageToken (a batch response can page). Missing
        projects simply don't appear in the result.
        """
        out: Dict[str, dict] = {}
        payload: dict = {
            "requests": [{"projectKey": {"id": pid}} for pid in project_ids],
        }
        while True:
            data = await self._request(
                "POST", f"{API_BASE}/projectbatch", payload,
            )
            if data is None:
                return out
            for item in data.get("responses", []):
                project = item.get("project")
                if not project:
                    continue
                pid = (
                    (item.get("request") or {}).get("projectKey", {}).get("id")
                    or (project.get("projectKey") or {}).get("id")
                )
                if pid:
                    out[pid.lower()] = project
            token = data.get("nextPageToken")
            if not token:
                return out
            payload["pageToken"] = token

    async def project_package_versions(self, project_id: str) -> List[dict]:
        data = await self._request(
            "GET",
            f"{API_BASE}/projects/{quote(project_id, safe='')}:packageversions",
        )
        return (data or {}).get("versions", [])

    async def get_package(self, system: str, name: str) -> Optional[dict]:
        return await self._request(
            "GET",
            f"{API_BASE}/systems/{quote(system, safe='')}"
            f"/packages/{quote(name, safe='')}",
        )

    async def dependent_count(
        self, system: str, name: str, version: str,
    ) -> Optional[int]:
        data = await self._request(
            "GET",
            f"{API_BASE}/systems/{quote(system, safe='')}"
            f"/packages/{quote(name, safe='')}"
            f"/versions/{quote(version, safe='')}:dependents",
        )
        if data is None:
            return None
        count = data.get("dependentCount")
        return int(count) if count is not None else None


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

_FETCH_REPOS_SQL = """
SELECT id, full_name, name
FROM repositories
WHERE is_archived = FALSE
ORDER BY stars DESC
LIMIT $1
"""

_FETCH_STALE_FILTER_SQL = """
SELECT repo_id, fetched_at FROM repository_signals
WHERE repo_id = ANY($1::text[])
  AND fetched_at > NOW() - make_interval(days => $2)
"""

_UPSERT_SCORECARD_SQL = """
INSERT INTO repository_signals (repo_id, scorecard_score, scorecard_date, fetched_at)
VALUES ($1, $2, $3, NOW())
ON CONFLICT (repo_id) DO UPDATE SET
    scorecard_score = EXCLUDED.scorecard_score,
    scorecard_date  = EXCLUDED.scorecard_date,
    fetched_at      = NOW()
"""

_UPSERT_DEPENDENTS_SQL = """
INSERT INTO repository_signals (repo_id, dependent_count, dependent_package, fetched_at)
VALUES ($1, $2, $3, NOW())
ON CONFLICT (repo_id) DO UPDATE SET
    dependent_count   = EXCLUDED.dependent_count,
    dependent_package = EXCLUDED.dependent_package,
    fetched_at        = NOW()
"""


# ---------------------------------------------------------------------------
# The passes
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    scorecards: int = 0
    dependents_found: int = 0
    no_packages: int = 0


async def _scorecard_pass(
    client: DepsDevClient, pool: asyncpg.Pool,
    repos: List[asyncpg.Record], stats: _Stats,
) -> Dict[str, dict]:
    """Batch-resolve every repo; store scorecards; return the projects
    deps.dev knows (keyed by lowercase project id) for pass 2."""
    known: Dict[str, dict] = {}
    for i in range(0, len(repos), PROJECT_BATCH_SIZE):
        chunk = repos[i:i + PROJECT_BATCH_SIZE]
        ids = [f"github.com/{r['full_name']}" for r in chunk]
        projects = await client.project_batch(ids)
        known.update(projects)

        rows = []
        for r in chunk:
            project = projects.get(f"github.com/{r['full_name']}".lower())
            if not project:
                continue
            scorecard = project.get("scorecard") or {}
            score = scorecard.get("overallScore")
            date_raw = (scorecard.get("date") or "")[:10] or None
            date = (
                datetime.date.fromisoformat(date_raw) if date_raw else None
            )
            if score is None and date is None:
                continue
            rows.append((r["id"], float(score) if score is not None else None,
                         date))
        if rows:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(_UPSERT_SCORECARD_SQL, rows)
            stats.scorecards += len(rows)
        logger.info(
            "  scorecard batch %d-%d: %d known, %d with scorecards",
            i, i + len(chunk), len(projects), len(rows),
        )
    return known


async def _dependents_for_repo(
    client: DepsDevClient, repo: asyncpg.Record,
) -> Optional[Tuple[int, str]]:
    """(max dependentCount, 'system:name') across the repo's packages,
    or None if it has no queryable published package."""
    project_id = f"github.com/{repo['full_name']}"
    versions = await client.project_package_versions(project_id)
    packages = select_packages(versions, repo["name"])
    if not packages:
        return None

    best: Optional[Tuple[int, str]] = None
    for system, name in packages:
        package = await client.get_package(system, name)
        if not package:
            continue
        label = f"{system.lower()}:{name}"
        for version in sample_versions(package):
            count = await client.dependent_count(system, name, version)
            if count is None:
                continue
            if best is None or count > best[0]:
                best = (count, label)
    return best


async def _dependents_pass(
    client: DepsDevClient, pool: asyncpg.Pool,
    repos: List[asyncpg.Record], workers: int, stats: _Stats,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    for repo in repos:
        queue.put_nowait(repo)
    total = len(repos)
    done = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal done
        while True:
            try:
                repo = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                best = await _dependents_for_repo(client, repo)
                if best is not None:
                    await pool.execute(
                        _UPSERT_DEPENDENTS_SQL, repo["id"], best[0], best[1],
                    )
                    stats.dependents_found += 1
                else:
                    stats.no_packages += 1
            except RuntimeError as exc:
                logger.warning("  %s: %s", repo["full_name"], exc)
            async with lock:
                done += 1
                if done % 500 == 0 or done == total:
                    logger.info(
                        "  dependents %d/%d (%d found, %d no-package)",
                        done, total, stats.dependents_found, stats.no_packages,
                    )

    await asyncio.gather(*(worker() for _ in range(workers)))


async def _run(args: argparse.Namespace) -> None:
    pool = await create_pool()
    stats = _Stats()
    started = time.monotonic()
    try:
        scope = 10_000_000 if args.scorecard_all else args.top_n
        repos = await pool.fetch(_FETCH_REPOS_SQL, scope)
        logger.info("Scorecard batch pass over %d repos.", len(repos))

        connector = aiohttp.TCPConnector(limit=args.workers * 2)
        async with aiohttp.ClientSession(connector=connector) as session:
            client = DepsDevClient(session)
            known = await _scorecard_pass(client, pool, repos, stats)

            dep_repos = [
                r for r in repos[:args.top_n]
                if f"github.com/{r['full_name']}".lower() in known
            ]
            if args.max_age_days:
                fresh = await pool.fetch(
                    _FETCH_STALE_FILTER_SQL,
                    [r["id"] for r in dep_repos], args.max_age_days,
                )
                fresh_ids = {
                    row["repo_id"] for row in fresh
                }
                before = len(dep_repos)
                dep_repos = [r for r in dep_repos if r["id"] not in fresh_ids]
                logger.info(
                    "  skipping %d repos refreshed within %d days",
                    before - len(dep_repos), args.max_age_days,
                )
            logger.info(
                "Dependents pass over %d known repos (top %d).",
                len(dep_repos), args.top_n,
            )
            await _dependents_pass(client, pool, dep_repos, args.workers, stats)
    finally:
        await pool.close()

    logger.info(
        "deps.dev pass done in %.0fs: %d scorecards, %d dependent counts, "
        "%d repos without packages.",
        time.monotonic() - started, stats.scorecards,
        stats.dependents_found, stats.no_packages,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest deps.dev signals into repository_signals.",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Repos (by stars desc) in the dependents pass.")
    parser.add_argument("--scorecard-all", action="store_true",
                        help="Run the scorecard batch pass over the whole "
                             "corpus, not just --top-n.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-age-days", type=int, default=0,
                        help="Skip repos whose signals are fresher than this "
                             "(0 = refresh everything).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
