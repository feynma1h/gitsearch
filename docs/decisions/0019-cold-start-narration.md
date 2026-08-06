# ADR 0019: Cold-start budget and honest wake narration

**Status:** accepted
**Date:** 2026-08-06

## Context

Both Cloud Run services scale to zero when idle, so the first search
after a quiet spell pays the full wake chain before the user sees a
result. The frontend narrates that wait against a persistent elapsed
clock (`COLD_STAGE_AT` in `frontend/index.html`), and the narration was
tuned to a verified pre-v2 cold search of 45.4s: copy promised "about
40 seconds", with a "taking longer than we expected" valve at 55s.

Search v2 (ADR 0018) made the first query more expensive: the three
retrieval lanes read GIN, pg_trgm, and halfvec HNSW indexes that are
not in Postgres's caches after a quiet spell, adding roughly 9–11s of
first-query warmup. The first post-v2 cold search observed in the wild
ran ~60s end to end — through the "Almost ready" stage, past the 55s
valve, results arriving only after the narration had run out of story.

Two full-cold measurements (Cloud Logging decomposition; the probe was
a curl pair mirroring the browser exactly — CORS preflight, then POST —
after ≥5h idle):

| Hop                                        | 02:40Z (user) | 07:39Z (probe) |
| ------------------------------------------ | ------------- | -------------- |
| Search container wake (paid by preflight)  | 10.7s         | 12.9s          |
| Embedding wake: instance spin-up           | 2.3s          | 2.8s           |
| Embedding wake: torch/model imports        | 27.5s         | 30.5s          |
| Embedding wake: model load (HF download)   | 8.0s          | 8.4s           |
| First-query index warmup (three-lane SQL)  | 11.2s         | 8.6s           |
| **Perceived total from click**             | **~60.5s**    | **64.7s**      |

Configuration already rules out the cheap fixes: startup CPU boost is
enabled on both services, and the embedding service already defers its
heavy imports out of module scope (uvicorn is accepting in ~2.8s) — the
import cost is serialized into the first request no matter when it
runs, so a lazy-import refactor cannot shorten the wait.

## Decision

Narrate the measured reality instead of buying it away. The wake story
now promises "about a minute", and the stages anchor to the physical
phases of the wake:

| Stage           | At    | Anchored to                                  |
| --------------- | ----- | -------------------------------------------- |
| "Waking up…"    | 2.5s  | past the flicker threshold                   |
| "About halfway" | 30s   | ~48% of the 60–65s envelope                  |
| "Almost ready…" | 50s   | where the results query actually starts      |
| "Taking longer" | 78s   | honesty valve, past the envelope             |

The client abort timer rises from 75s to 90s to match the server-side
request cap (`gcloud run --timeout=90`), so the browser waits exactly
as long as the server can possibly answer, and the silent-retry path
only fires when the server itself has given up.

The narration constants are a mirror of measured production behavior.
Whenever any layer of the wake changes — min-instances, an ONNX
migration, new indexes, phase-2 enrichment — re-measure one real cold
search and re-time the stages; this file records how.

## Alternatives considered

- **min-instances=1 on the embedding service** (~$8/mo at published
  idle rates for 1 vCPU / 2Gi). Removes the ~40s embedding wake; a
  cold search becomes ~13s container wake + ~10s index warmup ≈ 25s.
  Warming both services (~$15/mo) leaves only index warmup, ~12s.
  Deferred: it is the only lever that costs standing money, so it
  needs an explicit owner decision — and it still doesn't touch the
  Postgres cache warmup.
- **Keep-warm pinger** (Cloud Scheduler or a GitHub Actions cron
  hitting `/search` every ~10 min, ~$0). Keeps both Cloud Run
  instances *and* the Postgres caches hot, eliminating the cold path
  for nearly all visitors. Deferred for the same sign-off reason:
  this repo's scheduled automation was deliberately disabled
  (2026-07-29), and a pinger is standing automation plus synthetic
  traffic.
- **Lazy-import refactor.** Measured no-op — see Context.
- **ONNX runtime for the embedding service.** Dropping torch is the
  one code change that genuinely shrinks the wake: the ~30s import
  phase becomes ~1–2s and model load quickens, putting a cold search
  near ~25s with no standing cost. Costs a real migration: export
  bge-small to ONNX and verify embedding parity against the 244K
  stored vectors before serving. The right shape for the fix if the
  wake itself must shrink without spend.
- **Bake the model into the image.** The Dockerfile installs
  dependencies but never downloads the model, so every cold start
  fetches bge-small from Hugging Face (~8s of the wake, inside the
  "model load" hop). Baking it in pins the model snapshot and removes
  a hard runtime dependency on HF availability — today an HF outage
  breaks every cold start. Worth doing on the next embedding-service
  deploy regardless of the latency question.

## Consequences

- The story the user reads matches what actually happens, including
  which phase the engine is really in when each stage shows.
- The valve returns to being rare instead of routine.
- If a future change makes the wake faster, the narration overshoots
  (stages linger) until re-timed — annoying but honest, and this ADR
  says to re-measure on any wake-affecting change.
- The 60–65s envelope has ~13s of headroom before the 78s valve and
  ~25s before the client abort; a pathologically slow cold start is
  narrated truthfully the whole way down.

## What would change this decision

Any purchase of warmth (min-instances, pinger) or the ONNX migration —
each shrinks the wake enough that the narration, and possibly the whole
staged-wait design, should be re-derived from a fresh measurement.

## Addendum (2026-08-06, later): keep-warm pinger adopted

The owner declined standing spend (min-instances stays 0) and approved
the keep-warm pinger. One Cloud Scheduler job now POSTs a fixed
realistic query to `/search` every 10 minutes, exercising the search
service, the embedding service, and the three-lane SQL — which keeps
both Cloud Run instances and the Postgres index caches hot:

```
gcloud scheduler jobs create http keep-warm-search \
  --project=gitsearch-495722 \
  --location=asia-southeast1 \
  --schedule="*/10 * * * *" \
  --uri="https://gitsearch-search-148185858207.asia-southeast1.run.app/search" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"query":"self hosted note taking app","limit":20,"filters":{"exclude_archived":true},"weights":{"similarity":1.0,"stars":0.3,"recency":0.2}}' \
  --attempt-deadline=120s
```

Cost ≈ $0: one job sits inside Cloud Scheduler's three-free-jobs
allowance (else $0.10/mo), and ~4,400 pings/month cost ~$0.13 at list
rates, inside Cloud Run's free tier. The 120s attempt deadline
outlasts the 90s server request cap, so a ping that lands on a
recycled instance completes the wake instead of timing out. Pinger
traffic is identifiable in request logs by its
`Google-Cloud-Scheduler` user agent. This is Cloud Scheduler, not
GitHub Actions — the repo's Actions crons remain deliberately
disabled.

First forced ping verified: HTTP 200. The narration keeps its 60–65s
timing as the safety net for what the pinger cannot prevent — platform
recycles on deploys and node maintenance. The same day's logs supplied
a third organic full-cold sample confirming the envelope: 9.8s
preflight + 55.5s POST (43.6s embedding wake) = 65.3s perceived, told
honestly by the re-timed stages.
