"""Read-only corpus audit: what got uploaded, and what's still pending.

Answers "did everything intended get in, and what would a re-run fetch?"
without re-crawling anything. Pure SELECTs — it never writes.

Each pipeline stage is resumable and only touches missing rows, so the
"pending" numbers below are exactly what re-running that stage will do:

    make readmes   -> fetches the README-pending rows
    make index     -> embeds the embedding-pending rows

Usage:
    DATABASE_URL=postgresql://... python scripts/audit_corpus.py
    python scripts/audit_corpus.py --top-n 20000   # match your readme/index budget
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

# Repo-wide metadata: how much is in `repositories`, and crawl freshness.
_METADATA_SQL = """
    SELECT
        COUNT(*)                                        AS total_repos,
        COUNT(*) FILTER (WHERE is_archived)             AS archived,
        MAX(crawled_at)                                 AS last_crawled_at
    FROM repositories
"""

# README progress within the *intended* set: the top-N non-archived repos by
# stars — exactly what `readme_pass` targets (ORDER BY stars DESC, skip
# archived). `pending` is what a re-run of `make readmes` will fetch.
_README_SQL = """
    WITH intended AS (
        SELECT readme_status, readme_fetched_at
        FROM repositories
        WHERE is_archived = FALSE
        ORDER BY stars DESC
        LIMIT $1
    )
    SELECT
        COUNT(*)                                                       AS intended,
        COUNT(*) FILTER (WHERE readme_fetched_at IS NOT NULL)          AS fetched,
        COUNT(*) FILTER (WHERE readme_fetched_at IS NULL)              AS pending,
        COUNT(*) FILTER (WHERE readme_status = 'ok')                   AS ok,
        COUNT(*) FILTER (WHERE readme_status = 'not_found')            AS not_found,
        COUNT(*) FILTER (WHERE readme_status = 'empty')                AS empty,
        COUNT(*) FILTER (WHERE readme_status = 'error')                AS error
    FROM intended
"""

# Embedding progress: the indexer embeds any non-archived repo whose README
# pass ran (readme_status IS NOT NULL) and that lacks an embedding — must
# mirror indexer/pipeline/db.py::_FETCH_PENDING_SQL or "pending" drifts from
# what `make index` will actually do.
_EMBED_SQL = """
    SELECT
        (SELECT COUNT(*) FROM repository_embeddings)                   AS embeddings,
        (SELECT COUNT(*)
           FROM repositories r
           LEFT JOIN repository_embeddings e ON e.repo_id = r.id
          WHERE r.readme_status IS NOT NULL
            AND r.is_archived = FALSE
            AND e.repo_id IS NULL)                                     AS pending
"""

# Incremental-crawl watermark (may not exist yet if migration 0005 is unapplied).
_WATERMARK_SQL = "SELECT last_metadata_crawl_at FROM crawl_state WHERE id = 1"


async def _audit(top_n: int) -> None:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        meta = await conn.fetchrow(_METADATA_SQL)
        readme = await conn.fetchrow(_README_SQL, top_n)
        embed = await conn.fetchrow(_EMBED_SQL)
        try:
            watermark = await conn.fetchval(_WATERMARK_SQL)
        except asyncpg.UndefinedTableError:
            watermark = "(crawl_state table not created — migration 0005 unapplied)"

        print("── Metadata (repositories) ─────────────────────────────")
        print(f"  total repos:        {meta['total_repos']:>9,}")
        print(f"  of which archived:  {meta['archived']:>9,}")
        print(f"  last crawled_at:    {meta['last_crawled_at']}")
        print(f"  crawl watermark:    {watermark}")

        pct = (readme["fetched"] / readme["intended"] * 100) if readme["intended"] else 0
        print(f"\n── READMEs (top {top_n:,} by stars, non-archived) ──────────")
        print(f"  intended:           {readme['intended']:>9,}")
        print(f"  fetched:            {readme['fetched']:>9,}  ({pct:.1f}%)")
        print(f"  PENDING (re-fetch): {readme['pending']:>9,}  <- `make readmes` will fetch these")
        print(f"    status ok:        {readme['ok']:>9,}")
        print(f"    status not_found: {readme['not_found']:>9,}")
        print(f"    status empty:     {readme['empty']:>9,}")
        print(f"    status error:     {readme['error']:>9,}  (retryable)")

        print("\n── Embeddings (repository_embeddings) ──────────────────")
        print(f"  embeddings stored:  {embed['embeddings']:>9,}")
        print(f"  PENDING (to embed): {embed['pending']:>9,}  <- `make index` will embed these")

        pending_total = readme["pending"] + embed["pending"]
        print("\n────────────────────────────────────────────────────────")
        if pending_total == 0:
            print("  ✔ Nothing pending — corpus is fully populated.")
        else:
            print(f"  {pending_total:,} rows still pending; re-run the stages above to finish.")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only corpus completeness audit.")
    parser.add_argument(
        "--top-n", type=int, default=20000,
        help="README/index budget to audit against (match your `make` runs; default 20000).",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_audit(args.top_n))
    except Exception as e:  # noqa: BLE001 - operational script, surface any error plainly
        print(f"audit_corpus error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
