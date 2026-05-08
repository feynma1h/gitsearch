# ADR 0009: HTTP+JSON over gRPC for the embedding service

**Status:** accepted
**Date:** 2026-05-03

## Context

The embedding service is a long-running process that exposes the
embedding model over a network interface. The indexer pipeline calls it
during bulk indexing; the future search API will call it on every user
query. Three reasonable transport choices:

1. **HTTP + JSON** (e.g., FastAPI).
2. **HTTP + protobuf** (manually serialized).
3. **gRPC** (HTTP/2 + protobuf + generated stubs).

## Decision

Use **HTTP + JSON** via FastAPI.

## Alternatives considered

- **gRPC.** Real wins for production-scale ML serving: binary wire format
  is faster to (de)serialize than JSON, HTTP/2 multiplexing reduces
  connection overhead, schemas are enforced. But:
  - Per-request inference time (~20ms on CPU) dominates serialization
    overhead (~1ms). gRPC's serialization win is ~5% of total latency.
  - Adds protobuf schemas, code generation, language-specific clients.
    Harder to debug (no `curl`), harder to inspect (no browser dev tools).
  - Major commercial ML APIs (OpenAI, Anthropic, Cohere) all expose
    HTTP+JSON as their primary public interface. These are the
    organizations with the strongest incentive to optimize serving cost
    at scale; their convergent choice is a strong signal that for
    "load model once, serve embeddings forever" workloads, the
    HTTP+JSON tradeoffs land in the right place.
- **HTTP + protobuf.** Worst of both worlds: protobuf complexity without
  HTTP/2's multiplexing benefits.

## Consequences

- ✅ Trivial to debug — `curl` works, browser dev tools work.
- ✅ Easy to expose to a frontend later (no proxy needed).
- ✅ FastAPI gives us OpenAPI docs, request validation via Pydantic, and
  async support for free.
- ⚠️ Higher per-request overhead than gRPC. Negligible at our scale.
- ⚠️ JSON encoding of float arrays is verbose (each float is ~20 chars
  vs 4 bytes binary). For 384-dim vectors, payload size grows from ~1.5KB
  to ~10KB. Still small enough not to matter.

## What would change this decision

- Real measured latency from serialization becomes a meaningful share of
  total request time (e.g., we move to GPU and inference drops to <2ms).
- Search QPS exceeds ~1000 and connection-establishment overhead becomes
  visible. Mitigated by HTTP keep-alive before resorting to gRPC.

## Implementation note

The wire format is an implementation detail of the service boundary, not
of the model. We can swap to gRPC later without touching the model code
or the database schema.
