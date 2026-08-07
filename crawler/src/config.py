"""Shared defaults for the crawler.

This module exists for the small set of values that are referenced from
multiple places and that an operator might reasonably want to tune.
Values that live alongside the code that uses them (e.g.,
``README_MAX_CHARS`` in ``readme_client.py``) are deliberately *not*
hoisted here — see ``docs/decisions/`` for the layered-config rationale.

If you find yourself adding a constant here that's used in only one
file, put it in that file instead. ``config.py`` is for shared things.
"""

from __future__ import annotations

# --- Metadata crawl --------------------------------------------------------

# Default lower bound on stars. At ~200 stars the population is roughly
# 280K repos as of 2026 (it has grown well past the original ~100K
# target); see ADR 0003 for the rationale and what would change this.
DEFAULT_MIN_STARS: int = 200

# Concurrent workers in the metadata crawl.
#
# 5 is the result of trial and error against GitHub's *secondary* rate
# limit, which is a separate, undocumented mechanism layered on top of
# the primary 5000 pts/hour budget. It triggers on patterns GitHub
# considers abusive — particularly too many concurrent requests — and
# is not visible in the GraphQL `rateLimit` block we use elsewhere.
#
# Earlier versions of this code defaulted to 15 workers and tripped the
# secondary limit catastrophically: most shards aborted within minutes.
# Dropping to 5 trades some crawl wall time (~25 min for the full ~267K
# corpus) for the crawl actually completing where 15 aborts. See ADR 0001.
DEFAULT_METADATA_WORKERS: int = 5


# --- README pass -----------------------------------------------------------

# Concurrent workers in the README pass. REST requests are cheaper to
# process server-side than GraphQL, so we run more workers here.
DEFAULT_README_WORKERS: int = 30

# Default max repos per README run. 20K hits the "demo-quality" target
# in ~4 hours on a single token; raise for fuller coverage at the cost
# of runtime.
DEFAULT_README_TOP_N: int = 20_000
