# ADR 0008: Source document construction for embedding

**Status:** accepted
**Date:** 2026-05-03

## Context

The indexer needs to convert each repository into a single text blob to
feed to the embedding model. What goes into that blob, in what order,
and how long it can be — these choices directly determine what queries
can find what repos. The crawler captures roughly six potentially
useful signals per repo:

- `full_name` (e.g. `tokio-rs/axum`)
- `description` (one-line tagline, often present)
- `primary_language` (e.g. `Rust`)
- `topics` (TEXT[], user-curated tags)
- `readme` (markdown, may be absent or huge)
- `stars`, `pushed_at`, etc. — *numeric/temporal*, handled by the
  ranking layer rather than the embedding layer.

The embedding model (bge-small-en-v1.5; ADR 0007) has a 512-token
context window — roughly 2000 characters of typical English. Anything
beyond that is silently truncated by the tokenizer.

## Decision

Build one document per repo with this layout:

```
{full_name}: {description}
Language: {primary_language}
Topics: {topics joined with commas}

{readme, truncated as needed}
```

Truncate the final string to `SOURCE_TEXT_MAX_CHARS = 2500` chars before
sending to the model. Compute a SHA-256 hash of the final text and
store it alongside the embedding for later change detection.

Missing fields are skipped cleanly (no empty `Language:` line if the
repo has no language; no blank trailing section if there's no README).

Implementation: `indexer/pipeline/document_builder.py`. Pure functions,
no I/O, fully unit-tested.

## Alternatives considered

- **Just the README.** Many repos have no README (`readme_status =
  not_found`) or have a terse "TODO: add a README" placeholder. The
  description + topics fields are far more reliable per repo, even
  if individually short. Excluding them would make our search degrade
  badly on the long tail of small repos.
- **README first, metadata at the end.** Truncation eats the tail. If
  metadata is at the bottom, a long-README repo loses its name,
  description, language, and topics — which are the highest-signal
  bytes per character we have. Putting them first guarantees they
  survive.
- **Synthesise prose** ("This is a Rust repository about widgets and
  graphics. The main file..."). Wastes tokens on connective words.
  bge-style models are trained on naturally-mixed-format text and
  handle the structured layout fine; no measured quality gain from
  prose-ifying.
- **Multiple separate embeddings per repo** (one for description, one
  for README) combined at query time. More flexible — could weight
  them per query — but doubles storage, doubles index size, doubles
  embedding cost, and adds a combination heuristic that needs its own
  ADR. Reach for this only if single-document retrieval is shown to
  fail on a measured query set.
- **Include `homepage_url` in the document.** Considered, dropped:
  URLs are mostly tokenisation noise (`https://`, domain segments)
  with little semantic content. The repo's name and description
  already capture what the homepage is about.
- **Different truncation caps.** A larger cap (e.g. 8000 chars) would
  be wasted: the model truncates at ~512 tokens regardless, and the
  bytes past that just cost network and serialization time. A smaller
  cap (e.g. 1000 chars) cuts meaningful README content in half. The
  2500 cap is just slightly above the model's effective window — a
  small buffer for tokenisation surprises, no further slack.

## Consequences

- ✅ Truncation-safe: even a hostile 100KB README can't displace the
  metadata header, which is where the bulk of the searchable signal
  lives for most queries.
- ✅ Robust to missing fields: a repo with no README and no topics
  still produces a sensible document containing its name, description,
  and language.
- ✅ One embedding per repo, one row in `repository_embeddings`. Search
  is a clean nearest-neighbour lookup; no per-query combination logic.
- ✅ Pure function: testable in isolation, deterministic, no I/O.
  `tests/test_document_builder.py` pins down the truncation,
  field-skipping, and hash-stability behaviour.
- ⚠️ The format is a piece of policy. Changing it (reordering fields,
  changing the cap, including new fields) means re-embedding the
  corpus to keep search quality consistent. The `source_hash` makes
  this detectable but not automatic — see ADR 0006's "What would
  change this decision."
- ⚠️ Exact-name lookups ("redis" → `redis/redis`) work because
  `full_name` is in the document, but they work *as semantic search*,
  not as exact lookup. A user searching for "redis" by name can still
  be beaten by a repo whose description happens to embed closer. Pure
  dense retrieval has this limitation; a BM25 lane is the documented
  next step (ADR 0013).

## What would change this decision

- Measured search quality reveals a systematic blind spot tied to a
  specific field (e.g., topics aren't being weighted enough). Try
  repeating the topics line, or moving it ahead of the description.
- The embedding model is upgraded to one with a larger context window
  (e.g., `nomic-embed-text-v1.5` at 8192 tokens). Raise
  `SOURCE_TEXT_MAX_CHARS` correspondingly and reconsider whether
  truncation order still matters as much.
- We add per-field weighting via separate embeddings and learn the
  combination. At that point this single-document construction
  becomes one of several embedding strategies, and the choice between
  them gets its own ADR.
