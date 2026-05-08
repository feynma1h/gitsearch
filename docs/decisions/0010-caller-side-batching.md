# ADR 0010: Caller-side batching for embeddings

**Status:** accepted
**Date:** 2026-05-03

## Context

Embedding inference has high fixed overhead per call (model forward pass
setup, tokenization, GPU/CPU dispatch). Throughput grows roughly linearly
with batch size up to ~64 inputs. Two API shapes for the embedding service:

**Flavor 1 — Caller batches:**
```
POST /embed  {"texts": [t1, t2, ..., t32]}
→ {"embeddings": [v1, v2, ..., v32]}
```
The caller assembles batches; the server processes whatever it receives.

**Flavor 2 — Server-side dynamic batching:**
```
POST /embed  {"text": t}
→ {"embedding": v}
```
The server queues incoming requests with a short deadline (e.g., 10ms).
When the deadline fires or the queue is full, it batches the buffered
requests into one inference call and routes results back.

## Decision

**Flavor 1 (caller batches).** The service accepts a list of texts and
returns a list of vectors.

## Alternatives considered

- **Flavor 2.** The killer feature is automatic batching across many
  concurrent single-text callers — exactly what a high-traffic search
  API needs. But it adds queues, timers, and stateful failure modes
  (one bad input crashes a whole batch; queue overflow handling).
  We don't have a search API yet. Building Flavor 2 now solves a
  problem we don't have.
- **Hybrid: accept either shape.** Doubles the API surface for negligible
  benefit.

## Consequences

- ✅ Server is a stateless function: list of strings in, list of vectors
  out. Trivial to test, trivial to load-test.
- ✅ The indexer pipeline naturally has thousands of texts to process; it
  picks an optimal batch size (32) and gets good throughput.
- ✅ Easy migration to a paid API later — OpenAI's embedding endpoint has
  the same shape.
- ⚠️ When the search API is added, it will embed user queries one at a
  time. With Flavor 1, that's a 1-element "batch" — fine at low traffic,
  but doesn't auto-batch under concurrent load.

## What would change this decision

- Search QPS gets high enough that per-query inference time dominates
  total latency, AND many queries arrive concurrently (so dynamic
  batching would help). At that point implement Flavor 2 alongside
  Flavor 1, without removing it — keep the simple bulk path for the
  indexer.
