# Architecture Decision Records

This directory captures the significant design decisions made across
the project. Each ADR is a short markdown file explaining *what* was
decided and, more importantly, *why* — including the alternatives
considered and the conditions under which the decision should be
revisited.

ADRs are **append-only**. When a decision changes, write a new ADR
that supersedes the old one. Don't rewrite history; the point is to
preserve the reasoning at the time it was made.

## When to write one

Roughly: *would someone six months from now reasonably wonder why we
chose this, and could they get it wrong if they refactored without
knowing?* If yes, write an ADR. If no, a comment in the code suffices.

A reliable signal for "this needs an ADR" is when you find yourself
typing the second paragraph of a code comment to justify a choice —
that's the body of an ADR; lift it out.

## Format

Each ADR has these sections:

- **Status** — `accepted`, `superseded by ADR-NNNN`, or `deprecated`
- **Date** — when the decision was made
- **Context** — what problem prompted the decision
- **Decision** — what we chose
- **Alternatives considered** — what we rejected and why
- **Consequences** — what becomes easier (✅) and harder (⚠️)
- **What would change this decision** — concrete signals to revisit

The "alternatives considered" and "what would change this decision"
sections are the most valuable parts long-term; they're the difference
between an ADR and a commit message.

## Index

| ID   | Component | Title                                                                  | Status   |
| ---- | --------- | ---------------------------------------------------------------------- | -------- |
| 0001 | crawler   | [Sharded star-range crawling](0001-sharded-star-range-crawling.md)     | accepted |
| 0002 | crawler   | [REST API for README fetching](0002-rest-api-for-readmes.md)           | accepted |
| 0003 | crawler   | [Default min-stars threshold of 200](0003-min-stars-threshold.md)      | accepted |
| 0004 | crawler   | [`readme_status` enum](0004-readme-status-enum.md)                     | accepted |
| 0005 | indexer   | [Embedding model runs as a long-running service](0005-embedding-as-long-running-service.md) | accepted |
| 0006 | indexer   | [Separate `repository_embeddings` table](0006-separate-embeddings-table.md) | accepted |
| 0007 | indexer   | [`bge-small-en-v1.5` as the embedding model](0007-embedding-model-choice.md) | accepted |
| 0008 | indexer   | [Source document construction for embedding](0008-source-document-construction.md) | accepted |
| 0009 | indexer   | [HTTP+JSON over gRPC](0009-http-over-grpc.md)                          | accepted |
| 0010 | indexer   | [Caller-side batching](0010-caller-side-batching.md)                   | accepted |
| 0011 | indexer   | [HNSW over IVFFlat](0011-hnsw-vs-ivfflat.md)                           | accepted |
| 0012 | search    | [Search as a separate service](0012-search-as-a-separate-service.md)   | accepted |
| 0013 | search    | [Hybrid scoring formula and over-fetch + re-rank](0013-hybrid-scoring-formula.md) | superseded by 0018 |
| 0014 | pipeline  | [Chunked GitHub Actions for corpus refresh](0014-chunked-actions-refresh.md) | accepted |
| 0015 | crawler   | [Incremental metadata refresh](0015-incremental-metadata-refresh.md) | accepted |
| 0016 | search    | [LLM-generated repository usage guides](0016-llm-usage-guide.md) | accepted |
| 0017 | search    | [Agentic full-repo exploration for usage guides](0017-agentic-guide-generation.md) | accepted |
| 0018 | search    | [Three-lane hybrid retrieval with RRF fusion and an additive popularity blend](0018-three-lane-hybrid-retrieval.md) | accepted |

The indexer block (0005–0011) reads as a connected arc: *where the
model lives* → *where its output goes* → *what model produces it* →
*what text we feed the model* → *how we talk to it* → *how we batch
calls* → *how we index the result*. The search block (0012–0013)
builds on top.
