# ADR 0012: Search as a separate service

**Status:** accepted
**Date:** 2026-05-03

## Context

The search component takes a user query, embeds it, runs a hybrid
vector + metadata search against pgvector, and returns ranked repos.
Three reasonable shapes:

1. **Library imported by a bigger app.** A `search` package other code
   calls directly. No process boundary; whoever wants search owns the
   pool and the embedding client.
2. **Endpoints added to the embedding service.** The embedding service
   already loads the model and talks HTTP+JSON; tack `/search` onto it
   and the model is in-process for the search path too.
3. **Standalone FastAPI service** that talks to the embedding service
   over HTTP and to Postgres directly.

## Decision

Use **option 3**: a standalone FastAPI service in `search/`, sibling to
`crawler/` and `indexer/`, exposing `POST /search`.

## Alternatives considered

- **Option 1 (library).** Cleanest from a "no extra process" angle, but
  it forces every consumer to take on asyncpg + aiohttp + the
  embedding-service URL, and it precludes a thin frontend talking
  directly to search via HTTP. We will eventually want a UI, and a UI
  wants HTTP.
- **Option 2 (fold into embedding service).** Tempting because the
  model is right there — no network hop to embed the query. But:
  - Mixes concerns. The embedding service is a stateless function over
    a model; search is a stateful component with a DB pool, query
    parsing, ranking math. Coupling them complicates both.
  - Breaks the indexer's API contract (ADR 0010): the embedding service
    is `texts -> vectors`, full stop. Adding a `/search` endpoint there
    means the service now owns DB credentials and ranking logic, which
    bloats its dependency set and its blast radius.
  - Loses horizontal-scaling flexibility: search is I/O bound (DB +
    ranking); embedding is CPU bound (model inference). Different
    scaling profiles want different replica counts.
- **Search inside the indexer pipeline package.** Indexer is bulk;
  search is per-request user-facing. Different latency targets,
  different deployment shape, no shared logic worth deduplicating.

## Consequences

- ✅ Clear separation: each component (crawler, indexer, embedding
  service, search) has a single responsibility and its own ADR thread.
- ✅ Search can be deployed and scaled independently. The embedding
  service can run on a beefy CPU box; search can run on cheap small
  instances.
- ✅ HTTP-based — a frontend or a CLI client can hit it without going
  through Python.
- ✅ Mirrors the existing pattern (FastAPI for the embedding service)
  so contributors already know the shape.
- ⚠️ One extra HTTP hop per query (search → embedding service). At
  ~1-2ms localhost RTT and ~20ms inference, this is noise. If we
  ever co-locate them on the same host, we could collapse this with
  Unix sockets or in-process loading; not worth the complexity now.
- ⚠️ Two services to operate instead of one. Mitigated by the existing
  `docker-compose.yml`, which can grow a `search` service alongside
  the others.

## What would change this decision

- Search ends up needing the model in-process for some quality reason
  (e.g., re-ranking with a cross-encoder) and the network hop for
  embedding becomes a meaningful share of total latency.
