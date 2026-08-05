"""UMBRELA relevance judge over pooled run files.

Builds the judgment pool (union of the top-POOL_DEPTH results from every
run file given — the standard pooling protocol, so no compared system's
results go unjudged), finds (query, repo) pairs not yet in qrels.json,
fetches each repo's text from Postgres, and asks the judge model for a
0-3 relevance grade using the UMBRELA prompt (Upadhyay et al. 2024, the
TREC-adopted LLM assessor; system-ranking correlation with human qrels
holds up on small models). One (query, doc) pair per call, temperature 0.

Judgments are append-only: re-running only judges new pairs, so the
qrels grow monotonically as new system variants join the pool. The qrels
file is versioned in git alongside the judge prompt below — a grade is
only comparable to other grades from the same prompt + model family.

The judge model must come from a different family than any LLM in the
product pipeline (guides use Claude Haiku), so the default is Gemini.
Providers: gemini (default, GEMINI_API_KEY), openai (OPENAI_API_KEY),
anthropic (ANTHROPIC_API_KEY — fallback only; using the product's own
family weakens the eval claim and must be caveated in any writeup).

The repo text comes straight from Postgres via psql (DATABASE_URL), so
this script needs no third-party Python packages — subprocess + urllib
only, same no-deps policy as the rest of eval/.

Popularity fields (stars) are deliberately excluded from what the judge
sees: the judge grades relevance; ranking popularity is the engine's
job, and letting the judge see stars would bake popularity into the
ground truth twice.

Usage:
    # Preview: how many pairs need judging, and the rough cost.
    python -m eval.judge --runs runs/*.json --dry-run

    # Judge everything unjudged (incremental, crash-safe).
    python -m eval.judge --runs runs/*.json

    # Spot-check mode: print N random judged pairs for human review.
    python -m eval.judge --spot-check 20
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import glob
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_QRELS_PATH = Path(__file__).parent / "qrels.json"
POOL_DEPTH = 20          # judge the union of top-20 from every run (spec: prefer 20)
README_CHARS = 1500      # enough to know what a repo is; keeps cost ~$1 total
MAX_WORKERS = 24
SAVE_EVERY = 50          # incremental atomic saves — a crash loses at most this many

# One (query, passage) pair per call — batching pairs into one prompt is
# the documented way to degrade UMBRELA's calibration.
UMBRELA_PROMPT = """Given a query and a passage, you must provide a score on an \
integer scale of 0 to 3 with the following meanings:
0 = represent that the passage has nothing to do with the query,
1 = represents that the passage seems related to the query but does not answer it,
2 = represents that the passage has some answer for the query, but the answer may \
be a bit unclear, or hidden amongst extraneous information and
3 = represents that the passage is dedicated to the query and contains the exact answer.

Important Instruction: Assign category 1 if the passage is somewhat related to the \
topic but not completely, category 2 if passage presents something very important \
related to the entire topic but also has some extra information and category 3 if \
the passage only and entirely refers to the topic. If none of the above satisfies \
give it category 0.

Query: {query}
Passage: {passage}

Split this problem into steps:
Consider the underlying intent of the search.
Measure how well the content matches a likely intent of the query (M).
Measure how well the content satisfies the query (T).
Consider the aspects above and the relative importance of each, and decide on a \
final score (O). Final score must be an integer value only.
Do not provide any code in result. Provide each score in the format of: ##final \
score: score without providing any reasoning."""

# The queries are about software; the passage is a repository record. This
# framing note is the only adaptation to UMBRELA (which speaks of generic
# passages) — the scale and instructions above are verbatim.
PASSAGE_TEMPLATE = """GitHub repository record.
Name: {full_name}
Primary language: {language}
Topics: {topics}
Description: {description}
README (beginning):
{readme}"""

_SCORE_RE = re.compile(r"final\s*score\s*:?\s*\**\s*([0-3])", re.IGNORECASE)

# Pinned versions, not "-latest" aliases: a grade is only comparable to
# grades from the same judge, so the model that produced qrels.json must
# be re-runnable. (2.5-flash-lite was the original pick; Google closed
# it to new API projects in 2026 — any current lite-tier Gemini works,
# UMBRELA's ranking correlation holds on small models.)
PROVIDER_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}


# ---------------------------------------------------------------------------
# qrels I/O
# ---------------------------------------------------------------------------

def load_qrels(path: Path) -> dict:
    if path.exists():
        with path.open() as fh:
            return json.load(fh)
    return {
        "_comment": [
            "Pooled graded relevance judgments (qrels) for the v2 eval set.",
            "Produced by eval/judge.py (UMBRELA prompt, one pair per call,",
            "temperature 0). Append-only: never edit or delete a judgment to",
            "make a run look better; re-judging requires a version bump and",
            "a full re-judge. Keyed by exact query text, then repo full_name.",
        ],
        "prompt_version": "umbrela-v1",
        "judgments": {},
    }


def save_qrels_atomic(path: Path, qrels: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(qrels, fh, indent=1, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------

def build_pool(run_paths: List[str], depth: int) -> Dict[str, List[str]]:
    """Union of top-``depth`` results per query across all run files.
    Returns {query_text: [full_name, ...]} with first-seen ordering (order
    is irrelevant to judging; kept stable for reproducible dry-run output)."""
    pool: Dict[str, List[str]] = {}
    for path in run_paths:
        with open(path) as fh:
            run = json.load(fh)
        for query, retrieved in run["results"].items():
            bucket = pool.setdefault(query, [])
            for name in retrieved[:depth]:
                if name not in bucket:
                    bucket.append(name)
    return pool


def unjudged_pairs(
    pool: Dict[str, List[str]], qrels: dict
) -> List[Tuple[str, str]]:
    judged = qrels["judgments"]
    return [
        (q, name)
        for q, names in sorted(pool.items())
        for name in names
        if name not in judged.get(q, {})
    ]


# ---------------------------------------------------------------------------
# Repo text from Postgres (via psql — no Python DB driver needed)
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def fetch_docs(full_names: List[str]) -> Dict[str, dict]:
    """Fetch display fields + README head for each repo, one JSON row per
    line via row_to_json (which handles all escaping on the way out)."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set (source the repo .env).")
    for name in full_names:
        if not _SAFE_NAME_RE.match(name):
            raise SystemExit(f"unexpected characters in repo name: {name!r}")
    array_sql = ",".join(f"'{name}'" for name in full_names)
    sql = (
        "SELECT row_to_json(t) FROM ("
        "SELECT full_name, description, primary_language, topics, "
        f"left(coalesce(readme, ''), {README_CHARS}) AS readme_head "
        f"FROM repositories WHERE full_name = ANY(ARRAY[{array_sql}])"
        ") t;"
    )
    out = subprocess.run(
        ["psql", dsn, "-X", "-q", "-t", "-A"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise SystemExit(f"psql failed: {out.stderr.strip()}")
    docs: Dict[str, dict] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            docs[row["full_name"]] = row
    return docs


def build_passage(doc: dict) -> str:
    topics = doc.get("topics") or []
    return PASSAGE_TEMPLATE.format(
        full_name=doc["full_name"],
        language=doc.get("primary_language") or "(none recorded)",
        topics=", ".join(topics) if topics else "(none)",
        description=doc.get("description") or "(none)",
        readme=(doc.get("readme_head") or "(no README stored)").strip(),
    )


# ---------------------------------------------------------------------------
# Judge providers — plain urllib, retry on 429/5xx
# ---------------------------------------------------------------------------

def _post_json(url: str, headers: dict, payload: dict, attempts: int = 7) -> dict:
    body = json.dumps(payload).encode("utf-8")
    delay = 2.0
    for attempt in range(attempts):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < attempts - 1:
                # Rate limited. A free-tier key caps at ~10/minute, so
                # short exponential backoff just burns attempts — wait
                # a real window before retrying.
                time.sleep(max(delay, 20.0))
                delay *= 2
                continue
            if exc.code >= 500 and attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"judge API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"judge API unreachable: {exc}") from exc
    raise RuntimeError("unreachable")


def call_gemini(model: str, prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set (add it to the repo .env).")
    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 1024,
                # Flash-Lite has thinking off by default; pin it off so a
                # future default change can't silently alter the judge.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
    )
    parts = data["candidates"][0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def call_openai(model: str, prompt: str) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set (add it to the repo .env).")
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    return data["choices"][0]["message"]["content"]


def call_anthropic(model: str, prompt: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set (add it to the repo .env).")
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    return "".join(b.get("text", "") for b in data["content"])


PROVIDERS = {"gemini": call_gemini, "openai": call_openai, "anthropic": call_anthropic}


def judge_pair(provider: str, model: str, query: str, passage: str) -> Optional[int]:
    """One UMBRELA call; returns the 0-3 grade or None if unparseable
    after a retry (the caller skips — never guess a grade)."""
    prompt = UMBRELA_PROMPT.format(query=query, passage=passage)
    for _ in range(2):
        text = PROVIDERS[provider](model, prompt)
        m = _SCORE_RE.search(text)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="UMBRELA judge over pooled runs.")
    p.add_argument("--runs", nargs="+", default=[], help="Run files (globs ok).")
    p.add_argument("--qrels", type=Path, default=DEFAULT_QRELS_PATH)
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="gemini")
    p.add_argument("--model", help="Override the provider's default model.")
    p.add_argument("--pool-depth", type=int, default=POOL_DEPTH)
    p.add_argument("--dry-run", action="store_true",
                   help="Report pool size and unjudged count; judge nothing.")
    p.add_argument("--spot-check", type=int, metavar="N",
                   help="Print N random existing judgments for human review.")
    args = p.parse_args()

    qrels = load_qrels(args.qrels)

    if args.spot_check:
        rng = random.Random(1)
        flat = [
            (q, name, j)
            for q, docs in qrels["judgments"].items()
            for name, j in docs.items()
        ]
        for q, name, j in rng.sample(flat, min(args.spot_check, len(flat))):
            print(f"[{j['grade']}] {q!r:55s} {name}")
        return

    run_paths = [path for pattern in args.runs for path in sorted(glob.glob(pattern))]
    if not run_paths:
        raise SystemExit("no run files matched --runs")

    pool = build_pool(run_paths, args.pool_depth)
    todo = unjudged_pairs(pool, qrels)
    total_pairs = sum(len(v) for v in pool.values())
    print(f"pool: {len(pool)} queries, {total_pairs} pairs "
          f"({len(todo)} unjudged) from {len(run_paths)} runs")

    if args.dry_run or not todo:
        if todo:
            # ~700 tokens in + a handful out per pair; Flash-Lite pricing.
            print(f"estimated cost at gemini-2.5-flash-lite rates: "
                  f"~${len(todo) * 700 * 0.10 / 1e6:.2f}")
        return

    model = args.model or PROVIDER_MODELS[args.provider]
    if args.provider == "anthropic":
        print("WARNING: anthropic judge is the same model family as the "
              "product's guide pipeline; document this caveat and run "
              "--spot-check on ~50 pairs.", file=sys.stderr)
    qrels.setdefault("judge", {})
    qrels["judge"] = {"provider": args.provider, "model": model,
                      "prompt_version": qrels.get("prompt_version", "umbrela-v1")}

    names = sorted({name for _, name in todo})
    print(f"fetching {len(names)} repo docs from Postgres…")
    docs = fetch_docs(names)
    missing = [n for n in names if n not in docs]
    if missing:
        print(f"note: {len(missing)} pooled repos not in corpus "
              f"(deleted since crawl?) — judged as grade 0: {missing[:5]}…")

    lock = threading.Lock()
    done = 0
    started = time.monotonic()

    api_errors = 0

    def work(pair: Tuple[str, str]) -> None:
        nonlocal done, api_errors
        query, name = pair
        if name in docs:
            try:
                grade = judge_pair(
                    args.provider, model, query, build_passage(docs[name])
                )
            except RuntimeError as exc:
                # One pair failing (rate cap, transient API trouble)
                # must not kill the run — the pair stays unjudged and a
                # re-run picks it up. Surface the first few errors so a
                # systemic problem (bad key, free-tier quota) is
                # visible immediately.
                grade = None
                with lock:
                    api_errors += 1
                    if api_errors <= 3:
                        print(f"  API error (pair skipped): {exc}", file=sys.stderr)
        else:
            grade = 0  # not in corpus — cannot be relevant to serve
        with lock:
            if grade is not None:
                qrels["judgments"].setdefault(query, {})[name] = {
                    "grade": grade,
                    "model": model,
                    "judged_at": _dt.date.today().isoformat(),
                }
            done += 1
            if done % SAVE_EVERY == 0:
                save_qrels_atomic(args.qrels, qrels)
                rate = done / (time.monotonic() - started)
                eta = (len(todo) - done) / rate if rate > 0 else 0
                print(f"  {done}/{len(todo)} judged ({rate:.1f}/s, ~{eta:.0f}s left)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool_ex:
        list(pool_ex.map(work, todo))

    save_qrels_atomic(args.qrels, qrels)
    n_failed = len(todo) - sum(
        1 for q, n in todo if n in qrels["judgments"].get(q, {})
    )
    print(f"judged {len(todo) - n_failed}/{len(todo)} pairs "
          f"({n_failed} unparseable, skipped) -> {args.qrels}")


if __name__ == "__main__":
    main()
