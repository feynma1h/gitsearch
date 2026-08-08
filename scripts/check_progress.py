"""Progress probe for the corpus refresh pipeline.

Reports how many rows still need work in each stage and exits with a code
the GitHub Actions workflow can branch on:

    exit 0 = work remains in the requested stage (re-trigger)
    exit 1 = stage is complete (stop chunking)
    exit 2 = error (aborts the workflow)

Embedding counts are scoped to one model label (``EMBEDDINGS_MODEL_LABEL``,
defaulting to the base encoder) because ``repository_embeddings`` stores
several generations of vectors side by side.

Usage:
    python scripts/check_progress.py --stage readme
    python scripts/check_progress.py --stage index
    python scripts/check_progress.py --stage report   # prints all counts, always exits 0
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg


# repository_embeddings holds one row per (repo, label), so every count
# touching it must name a label or it silently answers about a different
# corpus than the one being served. Same env var and default the indexer
# pipeline and the search service use, so the probe measures exactly what
# `make index` would do next.
MODEL_LABEL = os.getenv("EMBEDDINGS_MODEL_LABEL", "BAAI/bge-small-en-v1.5")

# Each stage has a "remaining work" query. Tune thresholds here, not in YAML.
# The index query mirrors pipeline/db.py's pending query term for term —
# if they drift, chunking either stops early or never stops.
_QUERIES = {
    "readme": """
        SELECT COUNT(*) FROM repositories
        WHERE readme_fetched_at IS NULL
          AND is_archived = FALSE
    """,
    "index": """
        SELECT COUNT(*) FROM repositories r
        LEFT JOIN repository_embeddings e
               ON e.repo_id = r.id AND e.model_name = $1
        WHERE r.readme_status IS NOT NULL
          AND r.is_archived = FALSE
          AND e.repo_id IS NULL
    """,
}

_QUERY_ARGS = {"readme": (), "index": (MODEL_LABEL,)}

_REPORT_QUERY = """
    SELECT
        (SELECT COUNT(*) FROM repositories) AS total_repos,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status IS NOT NULL) AS readme_attempted,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status = 'ok') AS readme_success,
        (SELECT COUNT(*) FROM repository_embeddings
         WHERE model_name = $1) AS embeddings,
        (SELECT COUNT(*) FROM repository_embeddings) AS embeddings_all_labels,
        (SELECT MAX(crawled_at) FROM repositories) AS last_crawled_at
"""


async def _probe(stage: str) -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if stage == "report":
            row = await conn.fetchrow(_REPORT_QUERY, MODEL_LABEL)
            print(f"model_label:         {MODEL_LABEL}")
            print(f"total_repos:         {row['total_repos']:>8}")
            print(f"readme_attempted:    {row['readme_attempted']:>8}")
            print(f"readme_success:      {row['readme_success']:>8}")
            print(f"embeddings:          {row['embeddings']:>8}")
            print(f"  (all labels):      {row['embeddings_all_labels']:>8}")
            print(f"last_crawled_at:     {row['last_crawled_at']}")
            return 0

        remaining = await conn.fetchval(_QUERIES[stage], *_QUERY_ARGS[stage])
        print(f"{stage}_remaining: {remaining}")
        # GitHub Actions reads this for downstream conditionals.
        if gh_out := os.environ.get("GITHUB_OUTPUT"):
            with open(gh_out, "a") as f:
                f.write(f"remaining={remaining}\n")
        return 0 if remaining > 0 else 1
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["readme", "index", "report"])
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_probe(args.stage)))
    except Exception as e:
        print(f"check_progress error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
