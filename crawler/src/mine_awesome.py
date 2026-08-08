"""Mine awesome-list READMEs into ``repository_enrichment`` rows.

Usage:
    GITHUB_TOKEN=ghp_xxx DATABASE_URL=postgresql://... \\
        python -m src.mine_awesome [--dry-run] [--limit-lists N]

The corpus holds ~2.7K repos topic-tagged awesome/awesome-list. Each is
a human-curated catalog: repo links with hand-written descriptions
under category headings. This pass turns that curation into enrichment
rows (source='awesome-mined', migration 0009) — the anchor-text analog
web search has always leaned on, covering the head of the corpus with
exactly the category vocabulary ("Frameworks", "Machine Learning")
that canonical repos' own metadata lacks. No LLM involved.

Stored READMEs are capped at 8KB (readme_client.py), which beheads a
catalog file, so this pass re-fetches the *full* README of each list
transiently (REST budget: one call per list, ~2.7K calls total). Only
the mined entries are stored; ``repositories.readme`` is not touched.

The pass is a full re-mine each run and upserts wholesale per
(repo_id, 'awesome-mined'), so it is idempotent. Rows for repos that
dropped out of every list are only deleted with --prune-stale (off by
default; destructive ops stay guarded).

Finish with ``make enrichment-terms``: the search lane probes the
pre-folded ``repository_enrichment_terms`` table (sql/0011), not the
rows this pass writes, and nothing rebuilds it automatically — until it
runs, freshly mined enrichment changes no ranking.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp
import asyncpg

from .awesome_parser import MinedEntry, parse_awesome_readme
from .db import create_pool
from .rate_limiter import RateLimiter
from .readme_client import ReadmeClient

logger = logging.getLogger(__name__)

# The miner revision, recorded as prompt_version on every row so a
# parser/aggregation change can be told apart from the data it wrote.
MINER_VERSION = "awesome-miner-v1"

# Full catalog files are typically 30-400KB; 1MB reads the whole thing
# for any sane list while still bounding a malformed monster.
FULL_README_MAX_CHARS = 1_000_000
FULL_README_DOWNLOAD_CAP = 1_024 * 1_024

DEFAULT_WORKERS = 20

# --- Per-repo aggregation caps ---------------------------------------------
# A canonical repo appears in dozens of lists; the enrichment row must
# stay a compact document, not a dumping ground. Selection prefers
# descriptions that *differ* from the repo's own GitHub description
# (independent phrasings are the value; the GitHub description is
# already indexed), then frequency, then length.
MAX_DESCRIPTIONS = 8
MAX_DESCRIPTION_TOTAL_CHARS = 1_200
MAX_ALIASES = 8
MAX_CATEGORIES = 24

# Anchor texts that are link plumbing, not names.
_GENERIC_ALIASES = frozenset({
    "api", "app", "awesome", "blog", "cli", "client", "code", "demo",
    "docs", "documentation", "download", "example", "examples",
    "extension", "framework", "github", "guide", "gui", "here", "home",
    "home page", "homepage", "library", "link", "mirror", "more",
    "official", "official site", "page", "paper", "plugin", "project",
    "read more", "repo", "repository", "server", "site", "source",
    "source code", "tutorial", "web", "website",
})

_NORM_RE = re.compile(r"[\s\-._]+")

# What an alias may look like. Aliases index at weight A — name level —
# so junk here is the most expensive junk there is. ASCII name-ish
# strings only: "Dear ImGui", "ASP.NET Core", "C++", "scikit-learn"
# pass; "🦜️🔗 LangChain", "[code", "@handle", "Source ⭐ 311K" don't.
_ALIAS_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/&'_-]*$")
# When a repo appears in this many lists or more, an alias needs at
# least two independent votes: one curator's idiosyncratic (or plain
# wrong) anchor text must not become a name-weight match. Rarely-listed
# repos keep single votes — with two data points there is no quorum to
# demand.
_ALIAS_QUORUM_LISTS = 4


def _alias_ok(alias: str, count: int, n_lists: int) -> bool:
    if alias.lower() in _GENERIC_ALIASES:
        return False
    if not _ALIAS_SHAPE_RE.match(alias):
        return False
    if n_lists >= _ALIAS_QUORUM_LISTS and count < 2:
        return False
    return True


def _norm_key(text: str) -> str:
    """Case/punctuation-insensitive comparison key ("Next.js" == "nextjs")."""
    return _NORM_RE.sub("", text.lower())


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class _TargetAgg:
    """Everything every list said about one target repo."""
    descriptions: Dict[str, List] = field(default_factory=dict)  # norm -> [text, count]
    aliases: Counter = field(default_factory=Counter)            # display -> count
    categories: Counter = field(default_factory=Counter)
    n_lists: int = 0
    display_name: str = ""


def aggregate_entries(
    per_list_entries: Sequence[Tuple[str, List[MinedEntry]]],
) -> Dict[str, _TargetAgg]:
    """Fold every list's entries into per-target aggregates.

    Keyed by lowercased "owner/repo". Counts are per *list*, not per
    occurrence, so a list that mentions a repo five times gets one vote
    for ranking purposes (frequency across independent curators is the
    quality signal, self-repetition is not) — except descriptions,
    where distinct texts from one list are all kept as candidates.
    """
    targets: Dict[str, _TargetAgg] = {}
    for _source_list, entries in per_list_entries:
        seen_here: Dict[str, set] = {}
        for entry in entries:
            key = entry.full_name.lower()
            agg = targets.setdefault(key, _TargetAgg())
            if not agg.display_name:
                agg.display_name = entry.full_name
            marks = seen_here.setdefault(key, set())
            if "list" not in marks:
                marks.add("list")
                agg.n_lists += 1

            if entry.description:
                norm = _norm_key(entry.description)
                slot = agg.descriptions.setdefault(norm, [entry.description, 0])
                if ("desc", norm) not in marks:
                    marks.add(("desc", norm))
                    slot[1] += 1
            if entry.alias:
                alias_key = ("alias", entry.alias.lower())
                if alias_key not in marks:
                    marks.add(alias_key)
                    agg.aliases[entry.alias] += 1
            for category in entry.categories:
                cat_key = ("cat", category.lower())
                if cat_key not in marks:
                    marks.add(cat_key)
                    agg.categories[category] += 1
    return targets


def select_payload(
    agg: _TargetAgg,
    repo_name: str,
    repo_full_name: str,
    own_description: Optional[str],
) -> Tuple[Optional[str], List[str], List[str]]:
    """Reduce one target's aggregate to the stored (description,
    aliases, categories) under the module caps."""
    own_norm = _norm_key(own_description or "")

    ranked = sorted(
        agg.descriptions.values(),
        key=lambda slot: (
            _norm_key(slot[0]) != own_norm,   # independent phrasing first
            slot[1],                           # then breadth of agreement
            len(slot[0]),                      # then substance
        ),
        reverse=True,
    )
    chosen: List[str] = []
    total = 0
    for text, _count in ranked:
        if len(chosen) >= MAX_DESCRIPTIONS:
            break
        if total + len(text) > MAX_DESCRIPTION_TOTAL_CHARS and chosen:
            continue
        chosen.append(text)
        total += len(text)
    description = "\n".join(chosen) if chosen else None

    name_norms = {_norm_key(repo_name), _norm_key(repo_full_name)}
    aliases = [
        alias
        for alias, count in agg.aliases.most_common()
        if _alias_ok(alias, count, agg.n_lists)
        and _norm_key(alias) not in name_norms
    ][:MAX_ALIASES]

    categories = [
        category for category, _count in agg.categories.most_common()
    ][:MAX_CATEGORIES]

    return description, aliases, categories


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

_FETCH_LISTS_SQL = """
SELECT id, full_name, owner, name, stars
FROM repositories
WHERE topics && ARRAY['awesome', 'awesome-list']
  AND readme_status = 'ok'
ORDER BY stars DESC
LIMIT $1
"""

_RESOLVE_TARGETS_SQL = """
SELECT id, full_name, name, description
FROM repositories
WHERE lower(full_name) = ANY($1::text[])
"""

_UPSERT_SQL = """
INSERT INTO repository_enrichment
    (repo_id, source, description, queries, aliases, categories,
     model, prompt_version, generated_at)
VALUES ($1, 'awesome-mined', $2, '{}', $3, $4, NULL, $5, NOW())
ON CONFLICT (repo_id, source) DO UPDATE SET
    description    = EXCLUDED.description,
    queries        = EXCLUDED.queries,
    aliases        = EXCLUDED.aliases,
    categories     = EXCLUDED.categories,
    model          = EXCLUDED.model,
    prompt_version = EXCLUDED.prompt_version,
    generated_at   = NOW()
"""

_STALE_SQL = """
SELECT repo_id FROM repository_enrichment
WHERE source = 'awesome-mined' AND NOT (repo_id = ANY($1::text[]))
"""

_PRUNE_SQL = """
DELETE FROM repository_enrichment
WHERE source = 'awesome-mined' AND NOT (repo_id = ANY($1::text[]))
"""


async def _resolve_targets(
    pool: asyncpg.Pool, keys: List[str], chunk: int = 20_000
) -> Dict[str, asyncpg.Record]:
    """Map lowercased full_name -> corpus row, for targets we index."""
    out: Dict[str, asyncpg.Record] = {}
    for i in range(0, len(keys), chunk):
        rows = await pool.fetch(_RESOLVE_TARGETS_SQL, keys[i:i + chunk])
        for row in rows:
            out[row["full_name"].lower()] = row
    return out


# ---------------------------------------------------------------------------
# Fetch + orchestrate
# ---------------------------------------------------------------------------


async def _fetch_and_parse(
    client: ReadmeClient,
    semaphore: asyncio.Semaphore,
    owner: str,
    name: str,
    full_name: str,
) -> Tuple[str, List[MinedEntry]]:
    async with semaphore:
        result = await client.fetch(owner, name)
    if result.status != "ok" or not result.content:
        logger.debug("skip %s: %s", full_name, result.status)
        return full_name, []
    return full_name, parse_awesome_readme(result.content, full_name)


async def _run(args: argparse.Namespace) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required.")

    pool = await create_pool()
    started = time.monotonic()
    try:
        lists = await pool.fetch(_FETCH_LISTS_SQL, args.limit_lists)
        logger.info("Mining %d awesome lists (full-README refetch).", len(lists))

        limiter = RateLimiter(name="rest")
        semaphore = asyncio.Semaphore(args.workers)
        connector = aiohttp.TCPConnector(limit=args.workers * 2)
        async with aiohttp.ClientSession(connector=connector) as session:
            client = ReadmeClient(
                session, token, limiter,
                max_chars=FULL_README_MAX_CHARS,
                download_cap_bytes=FULL_README_DOWNLOAD_CAP,
            )
            tasks = [
                _fetch_and_parse(
                    client, semaphore, row["owner"], row["name"],
                    row["full_name"],
                )
                for row in lists
            ]
            per_list: List[Tuple[str, List[MinedEntry]]] = []
            done = 0
            for coro in asyncio.as_completed(tasks):
                per_list.append(await coro)
                done += 1
                if done % 250 == 0 or done == len(tasks):
                    logger.info("  fetched+parsed %d/%d lists", done, len(tasks))

        n_entries = sum(len(entries) for _, entries in per_list)
        parsed_lists = sum(1 for _, entries in per_list if entries)
        targets = aggregate_entries(per_list)
        logger.info(
            "Parsed %d entries from %d/%d lists; %d distinct link targets.",
            n_entries, parsed_lists, len(per_list), len(targets),
        )

        resolved = await _resolve_targets(pool, list(targets))
        logger.info(
            "%d targets resolve to corpus repos (%.0f%%).",
            len(resolved), 100 * len(resolved) / max(len(targets), 1),
        )

        rows = []
        for key, repo in resolved.items():
            agg = targets[key]
            description, aliases, categories = select_payload(
                agg, repo["name"], repo["full_name"], repo["description"],
            )
            if not description and not aliases and not categories:
                continue
            rows.append((
                repo["id"], description, aliases, categories, MINER_VERSION,
            ))

        logger.info("Prepared %d enrichment rows.", len(rows))
        _log_samples(rows, resolved, targets, args.sample)

        if args.dry_run:
            logger.info("--dry-run: not writing.")
            return

        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(_UPSERT_SQL, chunk)
            logger.info("  upserted %d/%d", min(i + 500, len(rows)), len(rows))

        written_ids = [row[0] for row in rows]
        stale = await pool.fetch(_STALE_SQL, written_ids)
        if stale:
            if args.prune_stale:
                await pool.execute(_PRUNE_SQL, written_ids)
                logger.info("Pruned %d stale awesome-mined rows.", len(stale))
            else:
                logger.info(
                    "%d previously-mined repos no longer appear in any list; "
                    "re-run with --prune-stale to delete their rows.",
                    len(stale),
                )
    finally:
        await pool.close()

    logger.info("Mining pass finished in %.1fs", time.monotonic() - started)


def _log_samples(rows, resolved, targets, n: int) -> None:
    """Log the head of the result by curation breadth — the repos every
    list agrees on, which is where enrichment must look sane."""
    if not rows or n <= 0:
        return
    by_id = {record["id"]: key for key, record in resolved.items()}
    ranked = sorted(
        rows, key=lambda r: targets[by_id[r[0]]].n_lists, reverse=True,
    )
    for repo_id, description, aliases, categories, _v in ranked[:n]:
        key = by_id[repo_id]
        logger.info(
            "  sample %s (in %d lists): aliases=%s categories=%s desc=%r",
            key, targets[key].n_lists, aliases[:4], categories[:6],
            (description or "")[:120],
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mine awesome-list READMEs into repository_enrichment.",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--limit-lists", type=int, default=10_000,
        help="Cap on lists to mine (by stars desc); the default covers all.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch, parse, aggregate, and report — write nothing.",
    )
    parser.add_argument(
        "--prune-stale", action="store_true",
        help="Delete awesome-mined rows for repos no longer in any list.",
    )
    parser.add_argument(
        "--sample", type=int, default=10,
        help="Log this many sample rows (most-listed first).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
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
