"""Detect post-refresh regressions in corpus health.

Compares current corpus counts against the watermark recorded by the last
successful refresh. Exit non-zero if any count decreased materially (5%+),
which fails the workflow and triggers GitHub's email alert.

Idempotent: if no watermark exists yet (first run), records the current
counts as the baseline and exits 0.

Usage:
    python scripts/check_regression.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


_REGRESSION_THRESHOLD = 0.95  # tolerate up to 5% drop (e.g., GitHub deletions)

_CURRENT_COUNTS = """
    SELECT
        (SELECT COUNT(*) FROM repositories)                      AS total_repos,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status = 'ok')                             AS readme_success,
        (SELECT COUNT(*) FROM repository_embeddings)             AS embeddings
"""


async def _check() -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        current = await conn.fetchrow(_CURRENT_COUNTS)
        prev = await conn.fetchrow("SELECT * FROM refresh_watermarks WHERE id = 1")

        print(f"current: total={current['total_repos']} "
              f"readme={current['readme_success']} "
              f"embeddings={current['embeddings']}")

        if prev is None:
            print("no prior watermark; recording baseline")
            await conn.execute(
                """
                INSERT INTO refresh_watermarks (id, total_repos, readme_success, embeddings)
                VALUES (1, $1, $2, $3)
                """,
                current["total_repos"], current["readme_success"], current["embeddings"],
            )
            return 0

        print(f"previous: total={prev['total_repos']} "
              f"readme={prev['readme_success']} "
              f"embeddings={prev['embeddings']} "
              f"(at {prev['recorded_at']})")

        regressions = []
        for col in ("total_repos", "readme_success", "embeddings"):
            if prev[col] == 0:
                continue
            ratio = current[col] / prev[col]
            if ratio < _REGRESSION_THRESHOLD:
                regressions.append(
                    f"{col} dropped from {prev[col]} to {current[col]} "
                    f"({ratio:.1%} of previous)"
                )

        if regressions:
            print("REGRESSION DETECTED:", file=sys.stderr)
            for r in regressions:
                print(f"  - {r}", file=sys.stderr)
            return 1

        # Healthy — update watermark.
        await conn.execute(
            """
            UPDATE refresh_watermarks
               SET total_repos = $1, readme_success = $2, embeddings = $3,
                   recorded_at = now()
             WHERE id = 1
            """,
            current["total_repos"], current["readme_success"], current["embeddings"],
        )
        print("watermark updated")
        return 0
    finally:
        await conn.close()


def main() -> None:
    try:
        sys.exit(asyncio.run(_check()))
    except Exception as e:
        print(f"check_regression error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()