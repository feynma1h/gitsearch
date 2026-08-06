"""LLM enrichment generation (source='llm') — Doc2Query-- style.

Usage:
    # 1. Estimate scope + cost (no API calls, no writes):
    python -m pipeline.enrich_llm --top-n 20000

    # 2. Submit the batch (SPENDS MONEY; requires explicit opt-in):
    ANTHROPIC_API_KEY=... python -m pipeline.enrich_llm --top-n 20000 \\
        --submit --i-approve-the-cost

    # 3. Collect finished batches, filter, and write enrichment rows
    #    (needs the embedding service up for the Doc2Query-- filter):
    python -m pipeline.enrich_llm --collect

Generates, per repo: 5-8 synthetic plain-English queries, a one-paragraph
"what is this for" description, name aliases, and category tags — the
category vocabulary canonical repos' own metadata lacks (ADR 0019; the
research plan's highest-leverage single change). Rows land in
``repository_enrichment`` under source='llm' with model + prompt_version
provenance, beside (never merged with) the awesome-mined rows.

Design notes:
  - **Message Batches API** (50% discount) with **structured outputs**
    (json_schema), so responses parse mechanically.
  - **Doc2Query-- filtering**: generated queries are embedded (same
    bge-small the corpus uses, via the local embedding service) and
    kept only if they actually match their source document
    (cosine >= QUERY_MIN_COSINE) — the "--" in Doc2Query--; unfiltered
    expansion measurably hurts (+16% with filtering in the original
    paper).
  - **Model choice**: default claude-haiku-4-5 — the project's
    established cheap-LLM tier (guides, ADR 0016), and NOT the eval
    judge's family (Gemini), preserving ADR 0018's judge-independence
    rule. Override with --model after checking pricing.
  - **Resumable / idempotent**: repos with an ('llm', PROMPT_VERSION,
    model) row are skipped at selection; batch ids are recorded in
    ``pipeline/.llm_batches.json`` so --collect can run any time within
    the API's 29-day result window.

The batch submission is deliberately hard to trigger by accident:
--submit does nothing without --i-approve-the-cost, and prints the
estimate either way. Keep it that way — corpus-scale LLM spend is a
user decision, not a default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp

from .client import EmbeddingClient
from .config import DEFAULT_SERVICE_URL, SOURCE_TEXT_MAX_CHARS
from .db import create_pool
from .document_builder import RepoForEmbedding, build_source_text

logger = logging.getLogger(__name__)

PROMPT_VERSION = "llm-enrich-v1"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TOP_N = 20_000

# Doc2Query-- consistency gate: a generated query must actually retrieve
# its own document. bge-small cosine between query and source doc;
# below this, the query is treated as drift/hallucination and dropped.
QUERY_MIN_COSINE = 0.45
MAX_QUERIES = 8
MIN_QUERIES_KEPT = 0          # a repo may end with fewer after filtering
MAX_ALIASES = 5
MAX_CATEGORIES = 6

BATCH_CHUNK = 10_000          # requests per Messages Batch (API max 100K)
STATE_FILE = Path(__file__).parent / ".llm_batches.json"

# Pricing per MTok for the estimate, batch rates (50% of standard).
# Source: claude-api skill pricing table, 2026-06. Keep in sync when
# changing DEFAULT_MODEL.
_BATCH_PRICES = {
    "claude-haiku-4-5": (0.50, 2.50),
    "claude-sonnet-5": (1.50, 7.50),
    "claude-opus-5": (2.50, 12.50),
}
# Rough per-repo token shape measured on representative docs: the
# system prompt + a ~2,000-char document in, a JSON payload out.
_EST_INPUT_TOKENS = 1_050
_EST_OUTPUT_TOKENS = 230

_SYSTEM_PROMPT = """You generate search-index enrichment for a GitHub \
repository search engine. Users type category phrases ("machine learning \
framework python"), task descriptions ("turn markdown into a website"), or \
half-remembered names; the engine indexes your output so canonical \
repositories match those phrasings.

Given one repository's metadata and README head, produce:
- queries: 5-8 plain-English searches a developer would type when THIS \
repository is the right answer. Mix category phrasings and task phrasings. \
Use the vocabulary of someone who does not know the repo's name; do not \
include the repo name in more than one query.
- description: one paragraph, at most 80 words: what it is, what you use \
it for, in plain category vocabulary (say "web framework" if it is one).
- aliases: up to 5 alternate names people actually use for it (renames, \
abbreviations, product names). Empty list if none exist.
- categories: 2-6 short category labels a directory would file it under \
("Web Framework", "Vector Database", "Static Site Generator").

Ground every claim in the provided text. Do not invent capabilities, \
integrations, or aliases that are not supported by it."""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array", "items": {"type": "string"},
        },
        "description": {"type": "string"},
        "aliases": {
            "type": "array", "items": {"type": "string"},
        },
        "categories": {
            "type": "array", "items": {"type": "string"},
        },
    },
    "required": ["queries", "description", "aliases", "categories"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_SELECT_SQL = """
SELECT r.id, r.full_name, r.description, r.primary_language, r.topics,
       r.readme
FROM repositories r
WHERE r.is_archived = FALSE
  AND r.readme_status IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM repository_enrichment en
      WHERE en.repo_id = r.id AND en.source = 'llm'
        AND en.prompt_version = $1 AND en.model = $2
  )
ORDER BY r.stars DESC
LIMIT $3
"""

_UPSERT_SQL = """
INSERT INTO repository_enrichment
    (repo_id, source, description, queries, aliases, categories,
     model, prompt_version, generated_at)
VALUES ($1, 'llm', $2, $3, $4, $5, $6, $7, NOW())
ON CONFLICT (repo_id, source) DO UPDATE SET
    description    = EXCLUDED.description,
    queries        = EXCLUDED.queries,
    aliases        = EXCLUDED.aliases,
    categories     = EXCLUDED.categories,
    model          = EXCLUDED.model,
    prompt_version = EXCLUDED.prompt_version,
    generated_at   = NOW()
"""


def _repo_document(row) -> str:
    """The text the model sees — the same base document the embedding
    pipeline uses (name/description/language/topics/README head), so
    generation and dense retrieval reason over identical evidence."""
    return build_source_text(RepoForEmbedding(
        full_name=row["full_name"],
        description=row["description"],
        primary_language=row["primary_language"],
        topics=list(row["topics"]) if row["topics"] else [],
        readme=row["readme"],
    ))[:SOURCE_TEXT_MAX_CHARS]


def estimate_cost(n_repos: int, model: str) -> Tuple[float, str]:
    prices = _BATCH_PRICES.get(model)
    if prices is None:
        return math.nan, f"unknown model {model!r} — add it to _BATCH_PRICES"
    cost = (
        n_repos * _EST_INPUT_TOKENS / 1e6 * prices[0]
        + n_repos * _EST_OUTPUT_TOKENS / 1e6 * prices[1]
    )
    detail = (
        f"{n_repos:,} repos x (~{_EST_INPUT_TOKENS} in @ ${prices[0]}/MTok "
        f"+ ~{_EST_OUTPUT_TOKENS} out @ ${prices[1]}/MTok, batch rates)"
    )
    return cost, detail


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _anthropic_client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "The 'anthropic' package is required for --submit/--collect: "
            "pip install anthropic"
        )
    return anthropic.Anthropic()


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"batches": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")


async def _select_pending(top_n: int, model: str):
    pool = await create_pool()
    try:
        return await pool.fetch(_SELECT_SQL, PROMPT_VERSION, model, top_n)
    finally:
        await pool.close()


def submit(rows, model: str) -> None:
    client = _anthropic_client()
    state = _load_state()

    for start in range(0, len(rows), BATCH_CHUNK):
        chunk = rows[start:start + BATCH_CHUNK]
        requests = []
        id_map: Dict[str, str] = {}
        for i, row in enumerate(chunk):
            # custom_id must be [a-zA-Z0-9_-]{1,64}; GraphQL node ids can
            # carry '=' padding, so use positional ids + a sidecar map.
            custom_id = f"r{start + i}"
            id_map[custom_id] = row["id"]
            requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": model,
                    "max_tokens": 1024,
                    "system": [{
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    "output_config": {
                        "format": {
                            "type": "json_schema",
                            "schema": _OUTPUT_SCHEMA,
                        },
                    },
                    "messages": [{
                        "role": "user",
                        "content": _repo_document(row),
                    }],
                },
            })
        batch = client.messages.batches.create(requests=requests)
        logger.info("submitted batch %s (%d requests)", batch.id, len(requests))
        state["batches"].append({
            "id": batch.id,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "id_map": id_map,
            "collected": False,
        })
        _save_state(state)


# ---------------------------------------------------------------------------
# Collect + Doc2Query-- filter
# ---------------------------------------------------------------------------


def _sane_strings(values: Sequence[str], cap: int, max_len: int) -> List[str]:
    out: List[str] = []
    for value in values:
        value = " ".join(str(value).split()).strip()
        if 0 < len(value) <= max_len and value.lower() not in (
            v.lower() for v in out
        ):
            out.append(value)
        if len(out) >= cap:
            break
    return out


async def _filter_queries(
    session: aiohttp.ClientSession,
    embedder: EmbeddingClient,
    doc_text: str,
    queries: List[str],
) -> List[str]:
    """Keep queries that retrieve their own document (cosine gate)."""
    if not queries:
        return []
    vectors = await embedder.embed([doc_text] + queries)
    doc_vec, query_vecs = vectors[0], vectors[1:]
    norm_d = math.sqrt(sum(x * x for x in doc_vec)) or 1.0
    kept = []
    for query, vec in zip(queries, query_vecs):
        norm_q = math.sqrt(sum(x * x for x in vec)) or 1.0
        cosine = sum(a * b for a, b in zip(doc_vec, vec)) / (norm_d * norm_q)
        if cosine >= QUERY_MIN_COSINE:
            kept.append(query)
        else:
            logger.debug("dropped query (cos=%.2f): %s", cosine, query)
    return kept


async def collect(model_hint: Optional[str]) -> None:
    client = _anthropic_client()
    state = _load_state()
    pending = [b for b in state["batches"] if not b["collected"]]
    if not pending:
        logger.info("No uncollected batches recorded in %s.", STATE_FILE)
        return

    pool = await create_pool()
    service_url = os.environ.get("EMBEDDING_SERVICE_URL", DEFAULT_SERVICE_URL)
    written = dropped_queries = 0
    try:
        async with aiohttp.ClientSession() as session:
            embedder = EmbeddingClient(session, service_url)
            for entry in pending:
                batch = client.messages.batches.retrieve(entry["id"])
                if batch.processing_status != "ended":
                    logger.info(
                        "batch %s still %s (%s processing) — skipping",
                        entry["id"], batch.processing_status,
                        batch.request_counts.processing,
                    )
                    continue
                repo_ids = entry["id_map"]
                doc_rows = await pool.fetch(
                    "SELECT id, full_name, description, primary_language, "
                    "topics, readme FROM repositories WHERE id = ANY($1::text[])",
                    list(repo_ids.values()),
                )
                docs = {row["id"]: _repo_document(row) for row in doc_rows}

                for result in client.messages.batches.results(entry["id"]):
                    if result.result.type != "succeeded":
                        logger.warning(
                            "%s: %s", result.custom_id, result.result.type,
                        )
                        continue
                    repo_id = repo_ids.get(result.custom_id)
                    if repo_id is None or repo_id not in docs:
                        continue
                    message = result.result.message
                    if message.stop_reason == "refusal":
                        continue
                    text = next(
                        (b.text for b in message.content if b.type == "text"),
                        "",
                    )
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning("%s: unparseable output", result.custom_id)
                        continue

                    queries = _sane_strings(
                        payload.get("queries", []), MAX_QUERIES, 120,
                    )
                    queries = await _filter_queries(
                        session, embedder, docs[repo_id], queries,
                    )
                    dropped_queries += len(
                        payload.get("queries", [])[:MAX_QUERIES]
                    ) - len(queries)
                    description = " ".join(
                        str(payload.get("description", "")).split()
                    )[:800] or None
                    aliases = _sane_strings(
                        payload.get("aliases", []), MAX_ALIASES, 40,
                    )
                    categories = _sane_strings(
                        payload.get("categories", []), MAX_CATEGORIES, 60,
                    )
                    if not (queries or description or aliases or categories):
                        continue
                    await pool.execute(
                        _UPSERT_SQL, repo_id, description, queries, aliases,
                        categories, entry["model"], entry["prompt_version"],
                    )
                    written += 1

                entry["collected"] = True
                _save_state(state)
                logger.info(
                    "batch %s collected: %d rows written so far "
                    "(%d queries dropped by the consistency filter)",
                    entry["id"], written, dropped_queries,
                )
    finally:
        await pool.close()
    logger.info("collect done: %d enrichment rows written.", written)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate LLM enrichment rows (Doc2Query--).",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--submit", action="store_true",
                        help="Submit the generation batch (costs money).")
    parser.add_argument("--i-approve-the-cost", action="store_true",
                        help="Required with --submit: confirms the printed "
                             "estimate was reviewed and approved.")
    parser.add_argument("--collect", action="store_true",
                        help="Collect finished batches and write rows.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.collect:
        asyncio.run(collect(args.model))
        return

    rows = asyncio.run(_select_pending(args.top_n, args.model))
    cost, detail = estimate_cost(len(rows), args.model)
    logger.info("Pending: %d repos without ('llm', %s, %s) rows.",
                len(rows), PROMPT_VERSION, args.model)
    logger.info("Estimated batch cost: $%.2f  [%s]", cost, detail)

    if not args.submit:
        logger.info("Estimate only. Re-run with --submit "
                    "--i-approve-the-cost to spend this.")
        return
    if not args.i_approve_the_cost:
        raise SystemExit(
            "--submit requires --i-approve-the-cost (the estimate above "
            "must be explicitly approved; see ADR 0019)."
        )
    if not rows:
        logger.info("Nothing to submit.")
        return
    submit(rows, args.model)


if __name__ == "__main__":
    main()
