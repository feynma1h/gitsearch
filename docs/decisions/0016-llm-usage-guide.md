# ADR 0016: LLM-generated repository usage guides

**Status:** accepted
**Date:** 2026-07-24

## Context

Search returns a ranked list of repos, but a user who finds a promising
result still has to open it, read the README, and work out how to actually
try it. A short, standard "how do I use this?" guide — what it is,
prerequisites, install, run, next step — turns a result into something
immediately actionable.

The material for that guide already exists: we store each repo's README.
Summarising it into a fixed format is exactly the kind of short-form text
task a small language model does well and cheaply.

## Decision

Add a `GET /guide/{repo_id}` endpoint to the **search service** that returns
a five-section Markdown guide, generated from the repo's stored README by
**Claude Haiku 4.5** and **lazily cached** in a `repository_guides` table
(migration 0006):

1. On request, look up the cached guide. Serve it if present and not stale
   (stale = the README has been re-fetched since the guide was generated,
   tracked via `source_readme_fetched_at`).
2. On a miss, generate the guide (`guide.generate_guide`), store it
   (`db.upsert_guide`), and return it.

The output shape is fixed by a system prompt (`## What it is` →
`## Prerequisites` → `## Install` → `## Run it` → `## Next step`) so the UI
renders every guide identically and the model can't wander into free-form
output. The prompt instructs the model to ground each step in the README and
to say plainly when the README doesn't cover a section, rather than invent
commands. The frontend shows a "how do I use this?" disclosure per result
that calls the endpoint on first open.

`ANTHROPIC_API_KEY` on the search service enables the endpoint; if it's
unset, the endpoint returns 503 and search is otherwise unaffected.

## Alternatives considered

- **On-demand, no cache.** Simplest, but pays the model on *every* click of
  the same repo — a popular result clicked 100× costs 100× for identical
  text. The lazy cache is on-demand *and* pay-once. Rejected.
- **Precompute a guide for every repo up front.** Wastes generation on the
  long tail of repos nobody clicks (~280K repos × ~$0.0065 ≈ $1,800 for
  content that mostly goes unread). Lazy generation only ever pays for repos
  a user actually opens. Rejected.
- **A separate microservice for guides.** More moving parts and another
  deploy target for one endpoint that reuses the search service's existing
  DB pool and rate limiter. Rejected; folded into the search service.
- **A larger model (Sonnet/Opus).** The task is short summarisation of text
  we already have; a small model is the right fit at a fraction of the cost.
  Revisit only if guide quality proves insufficient.

## Cost

Per generation (first click of a repo only, then free forever):

- Input: truncated README (~4K tokens) + a small prompt.
- Output: ~500 tokens (five terse sections).
- At Haiku 4.5 pricing ($1 / $5 per million in/out): **~$0.0065 per repo.**

Cost scales with *distinct repos users open*, not with traffic, and never
recurs for a repo once cached.

## Consequences

- ✅ Every result becomes actionable without leaving the app.
- ✅ Cost is bounded and pay-once; storage is a few KB of text per repo.
- ✅ No new service or deploy target; reuses the search service's pool,
   lifespan, and rate limiter (throttled harder than `/search` since a miss
   costs a model call).
- ✅ Guides regenerate automatically when a repo's README is refreshed.
- ⚠️ Guide quality is bounded by README quality — a repo with a thin README
   gets a thin guide. The prompt handles this by stating plainly where the
   README is silent rather than fabricating steps.
- ⚠️ Adds a runtime dependency on the Anthropic API (and a key) to the
   search service. Made optional: unset key → endpoint disabled, search
   unaffected.

## What would change this decision

- Guide quality proves insufficient on real repos → try a larger model or
  enrich the input beyond the README (topics, file tree, package manifests).
- Per-repo cost becomes material at higher scale → add a cheaper precompute
  path for the most-clicked repos, or batch generation.
- Guides need to be multilingual or personalised → the fixed single-format
  prompt is no longer enough; revisit the template.
