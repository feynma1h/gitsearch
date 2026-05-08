# ADR 0005: Embedding model runs as a long-running service

**Status:** accepted
**Date:** 2026-05-03

## Context

The indexer pipeline needs to turn ~20K repos into 384-dim vectors.
The chosen model (bge-small-en-v1.5; [ADR 0007](0007-embedding-model-choice.md))
loads in ~3 seconds on CPU and consumes ~500MB of RAM once loaded — a
non-trivial fixed cost per process. Three structural shapes for "where
does the model live?":

1. **In-process.** Import sentence-transformers directly into the
   pipeline. The pipeline starts up, loads the model, embeds, exits.
2. **Subprocess per pipeline run.** Pipeline spawns a worker process
   that loads the model, streams embedding requests over stdin/stdout
   or a Unix socket for the duration of the run, then dies with the
   pipeline.
3. **Long-running service.** A separate process (FastAPI app) that
   loads the model once at startup and serves `POST /embed` to anyone
   who asks. Dies only when explicitly stopped.

The search component, when it arrives later, will also need to embed
text (the user query). So the question isn't only "how does the
indexer pipeline get embeddings?" but "how do *all* current and future
embedding consumers get embeddings?"

## Decision

Run the embedding model as a **long-running service** in its own
process: `indexer/service/`. Pipeline and search both call it over
HTTP. The service loads the model in its FastAPI lifespan handler,
serves until stopped, and exposes `POST /embed` and `GET /health`.

## Alternatives considered

- **In-process load (option 1).** Simplest. Works fine for the indexer
  pipeline alone — load once at startup, amortise across the whole run.
  Falls apart the moment search arrives: the search service has a
  different lifecycle (always-on, not batch), and the indexer pipeline
  reruns (e.g., for a re-embedding pass) would each pay the 3-second
  load cost. More importantly, *both* processes would need PyTorch +
  sentence-transformers in their dependency tree — a ~500MB install
  added to a search service that otherwise only needs FastAPI and
  asyncpg.
- **Subprocess per run (option 2).** Decouples the model from the
  Python process that needs embeddings, but the model still dies when
  the pipeline does. Search would still need its own model load. And
  subprocess IPC is harder to debug than HTTP — no `curl`, no browser
  dev tools, no easy way to talk to it from anywhere except the parent.
- **Multiple services, one per consumer.** Indexer has its own service,
  search has its own. Doubles the operational footprint for no
  benefit; both consumers want exactly the same `text -> vector`
  function, just with different call patterns.

## Consequences

- ✅ Model loads once across the entire system. The 3-second cold start
  is paid by the operator at deploy time, not by every pipeline run
  and every search query.
- ✅ Heavy dependencies (PyTorch, sentence-transformers, ~500MB
  install) live in one place. The indexer pipeline and the search
  service both depend only on `aiohttp` to talk to it.
- ✅ Search queries don't need a Python process at all — a frontend
  could `fetch()` the embedding service directly if we ever wanted to
  expose it that way (we don't, but it's available).
- ✅ The service is a stateless function — `texts -> vectors` — which
  makes it trivial to test, trivial to replicate, and trivial to swap
  out (e.g., for a hosted API like OpenAI's embeddings) by changing
  one client.
- ⚠️ One more long-running process to operate. Mitigated by docker
  compose handling its lifecycle alongside Postgres.
- ⚠️ Cold start now happens at service startup, not at first request.
  Health checks need a generous `start_period` (60s in the compose
  file) so the service isn't killed for being slow on the first boot.
- ⚠️ Network hop for every embedding call. At ~1ms localhost RTT vs
  ~20ms inference, this is noise — but it does mean the service has
  to be reachable, which is the main thing that goes wrong in
  development. The pipeline's first action is a health check against
  the service so failures are loud, not silent.

## What would change this decision

- The indexer pipeline becomes the only consumer of embeddings (no
  search service, no other use). At that point in-process loading is
  simpler and the service is overhead.
- The model gets small enough that load time stops being a real cost
  (e.g., we move to a tiny distilled model that loads in <100ms). The
  case for long-running shrinks.
- We move to a hosted embeddings API (OpenAI, Voyage). The "service"
  becomes a thin proxy — at that point it might fold into the search
  service or disappear entirely, with each consumer calling the
  hosted API directly.
