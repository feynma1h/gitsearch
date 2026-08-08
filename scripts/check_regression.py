"""Detect post-refresh regressions in corpus health.

Compares current corpus counts against the watermark recorded by the last
successful refresh. Exit non-zero if any count decreased materially (5%+),
which fails the workflow and triggers GitHub's email alert.

Idempotent: if no watermark exists yet (first run), records the current
counts as the baseline and exits 0.

The embedding count is scoped to the serving model label
(``EMBEDDINGS_MODEL_LABEL``). ``repository_embeddings`` holds every
label's rows, and an all-label count answers the wrong question twice
over: retiring a parked label reads as a catastrophic loss, while a real
loss inside the serving label is diluted by the labels beside it — at
three labels stored, losing a seventh of what users actually search
stays under the 5% alarm. Pass ``--rebaseline`` to overwrite the
watermark without comparing; needed exactly once, when moving an existing
all-label watermark onto label-scoped counts.

Usage:
    python scripts/check_regression.py
    python scripts/check_regression.py --rebaseline
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg


_REGRESSION_THRESHOLD = 0.95  # tolerate up to 5% drop (e.g., GitHub deletions)

MODEL_LABEL = os.getenv("EMBEDDINGS_MODEL_LABEL", "BAAI/bge-small-en-v1.5")

_CURRENT_COUNTS = """
    SELECT
        (SELECT COUNT(*) FROM repositories)                      AS total_repos,
        (SELECT COUNT(*) FROM repositories
         WHERE readme_status = 'ok')                             AS readme_success,
        (SELECT COUNT(*) FROM repository_embeddings
         WHERE model_name = $1)                                  AS embeddings
"""

_UPSERT_WATERMARK = """
    INSERT INTO refresh_watermarks (id, total_repos, readme_success, embeddings)
    VALUES (1, $1, $2, $3)
    ON CONFLICT (id) DO UPDATE SET
        total_repos    = EXCLUDED.total_repos,
        readme_success = EXCLUDED.readme_success,
        embeddings     = EXCLUDED.embeddings,
        recorded_at    = now()
"""


async def _check(rebaseline: bool = False) -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        current = await conn.fetchrow(_CURRENT_COUNTS, MODEL_LABEL)
        print(f"model label: {MODEL_LABEL}")

        if rebaseline:
            print(f"current: total={current['total_repos']} "
                  f"readme={current['readme_success']} "
                  f"embeddings={current['embeddings']}")
            await conn.execute(
                _UPSERT_WATERMARK, current["total_repos"],
                current["readme_success"], current["embeddings"],
            )
            print("watermark re-baselined (no comparison performed)")
            return 0

        prev = await conn.fetchrow("SELECT * FROM refresh_watermarks WHERE id = 1")

        print(f"current: total={current['total_repos']} "
              f"readme={current['readme_success']} "
              f"embeddings={current['embeddings']}")

        if prev is None:
            print("no prior watermark; recording baseline")
            await conn.execute(
                _UPSERT_WATERMARK, current["total_repos"],
                current["readme_success"], current["embeddings"],
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
            print(
                "If this is an intended change to what's counted (a retired "
                "embedding label, a new EMBEDDINGS_MODEL_LABEL) rather than "
                "data loss, re-run with --rebaseline.",
                file=sys.stderr,
            )
            return 1

        # Healthy — update watermark.
        await conn.execute(
            _UPSERT_WATERMARK, current["total_repos"],
            current["readme_success"], current["embeddings"],
        )
        print("watermark updated")
        return 0
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebaseline", action="store_true",
        help="Overwrite the watermark with the current counts without "
             "comparing. For intended changes to what is counted, not for "
             "silencing a real regression.",
    )
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_check(args.rebaseline)))
    except Exception as e:
        print(f"check_regression error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
