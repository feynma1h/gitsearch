"""Progress probe for the corpus refresh pipeline.

Reports how many rows still need work in each stage and exits with a code
the GitHub Actions workflow can branch on:

    exit 0 = work remains in the requested stage (re-trigger)
    exit 1 = stage is complete (stop chunking)
    exit 2 = error (aborts the workflow)

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


# Each stage has a "remaining work" query. Tune thresholds here, not in YAML.
_QUERIES = {
    "readme": """
        SELECT COUNT(*) FROM repositories
        WHERE readme_status IS NULL
          AND stargazers_count >= 200
    """,
    "index": """
        SELECT COUNT(*) FROM repositories r
        LEFT JOIN repository_embeddings e ON e.repository_id = r.id
        WHERE r.readme_status = 'success'
          AND e.repository_id IS NULL
    """,
}

_REPORT_QUERY = """
    SELECT
        (SELECT COUNT(*) FROM repositories) AS total_repos,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status IS NOT NULL) AS readme_attempted,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status = 'success') AS readme_success,
        (SELECT COUNT(*) FROM repository_embeddings) AS embeddings,
        (SELECT MAX(updated_at) FROM repositories) AS last_metadata_update
"""


async def _probe(stage: str) -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        if stage == "report":
            row = await conn.fetchrow(_REPORT_QUERY)
            print(f"total_repos:         {row['total_repos']:>8}")
            print(f"readme_attempted:    {row['readme_attempted']:>8}")
            print(f"readme_success:      {row['readme_success']:>8}")
            print(f"embeddings:          {row['embeddings']:>8}")
            print(f"last_metadata_update:{row['last_metadata_update']}")
            return 0

        remaining = await conn.fetchval(_QUERIES[stage])
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