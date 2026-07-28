# ADR 0017: Agentic full-repo exploration for usage guides

**Status:** accepted
**Date:** 2026-07-29

## Context

ADR 0016's guides are generated from the stored README alone. That bounds
guide quality by README quality — and READMEs are written to *sell* a
project, not to operate it. The sections users actually need (`## Install`,
`## Run it`) often live elsewhere: the package manifest, `docs/quickstart`,
an `examples/` file, the Makefile. A README-only guide either paraphrases
marketing copy or honestly reports "the README doesn't document this" —
correct, but not useful.

The store can't help: only metadata and READMEs are crawled. Guides for
"the full repo" therefore mean fetching from GitHub at generation time.
The lazy-cache design makes that affordable — whatever it costs happens
once per repo, on the first click, and never again.

## Decision

On a cache miss, when `GITHUB_TOKEN` is set on the search service, the
guide model (still Claude Haiku 4.5) generates through a **bounded agentic
tool loop** instead of a single call. It gets two tools, implemented by a
new `repo_browser` module:

- `list_files` — the repo's file tree at `HEAD` via the REST trees API,
  capped at `GUIDE_TREE_MAX_ENTRIES` paths (shallow paths kept when
  cutting, so the top-level layout always survives), fetched at most once
  per generation.
- `read_file` — one file's content via `raw.githubusercontent.com`
  (quota-free; the token is deliberately not sent to that host), binary
  files rejected, text capped at `GUIDE_FILE_CHAR_LIMIT` characters.

The loop is the standard manual tool-use pattern with hard bounds: after
`GUIDE_MAX_TOOL_ROUNDS` model calls, the answer is forced with
`tool_choice: none`. The prompt keeps the fixed five-section format and
extends the grounding rule from "the README" to "the README or a file you
actually read". Using `HEAD` as the ref everywhere avoids ever resolving
the default branch.

Failure containment, in order of blast radius:

- A single tool failure (missing file, rate limit, network) returns an
  **error tool result**; the model keeps going and writes from what it has.
- No `GITHUB_TOKEN` at all → the ADR 0016 single-call README path is used
  unchanged. The token is optional the same way `ANTHROPIC_API_KEY` is.
- Only an Anthropic-side failure fails the request, exactly as before.

## Alternatives considered

- **Heuristic enrichment (no agency).** Fetch the tree, pick "important"
  files by path patterns, stuff them into one prompt. Simpler and
  deterministic, but the heuristics are the weak point: they'd be written
  once, generically, for 280K wildly different repos. The model reading
  the actual tree — and chasing the README's own "see docs/X" pointers —
  picks better files than any static pattern list. Rejected, though it
  remains the obvious fallback if loop behavior proves erratic.
- **Downloading repo tarballs.** One request for everything, but some
  repos are hundreds of MB and the service runs in 512Mi; streaming
  extraction to stay within memory is more complexity than the whole tool
  loop. Rejected.
- **The SDK's beta tool runner.** Handles the loop for us, but the tools
  close over per-request state (repo, session, token), the loop needs a
  hard round cap with a forced-answer ending, and the manual version is
  ~30 lines against a stable non-beta API. Rejected for now.
- **Unauthenticated GitHub access.** 60 requests/hour per egress IP,
  shared across everything behind Cloud Run's NAT. Dead on arrival.

## Cost and latency

Per first-click generation (cached forever after):

- **API quota:** one trees call per guide against a 5,000/hour token
  budget; `raw.githubusercontent.com` reads don't count against it.
- **Tokens:** conversation history grows across rounds; worst case
  (`8 rounds × ~20K-char files`) is roughly 60–80K cumulative input
  tokens ≈ **$0.07**, typical runs (2–4 reads) a few cents. README-only
  was ~$0.0065 — an order of magnitude more, on a per-click-once cost.
- **Latency:** ~10–20s worst case vs ~3–5s before. Acceptable because the
  frontend shows a loading state on first open and every later view is a
  cache hit.

## Consequences

- ✅ `## Install` and `## Run it` come from manifests, docs, and examples
  — the guide can now be *better* than the README instead of bounded by it.
- ✅ Degrades gracefully at every layer; the README-only path survives
  intact as both fallback and no-token mode.
- ✅ All bounds are config knobs (`GUIDE_MAX_TOOL_ROUNDS`,
  `GUIDE_TREE_MAX_ENTRIES`, `GUIDE_FILE_CHAR_LIMIT`).
- ⚠️ First-click latency and cost rise (numbers above). The cache and the
  existing `GUIDE_RATE_LIMIT` bound the exposure.
- ⚠️ A second secret on the service (`GITHUB_TOKEN`; fine-grained PAT,
  public read-only — separate from the crawler's token so either can be
  revoked alone).
- ⚠️ Guides generated before this ADR remain cached in their README-only
  form until their repo's README is next refreshed (the staleness rule is
  unchanged). Acceptable: they were correct when written; the weekly
  refresh naturally upgrades active repos over time.
- ⚠️ Fetched file content is untrusted input to the model, and a README
  could try to steer the loop ("read .env and include it"). Exposure is
  bounded: the browser reads public GitHub content only, the guide is
  plain rendered Markdown, and the loop has no write or network tools
  beyond the two readers.

## What would change this decision

- Loop behavior proves erratic (wasted reads, worse guides) → fall back
  to heuristic enrichment: same browser, curated file set, one call.
- Haiku under-uses the tools or misreads manifests → try Sonnet on the
  same loop; the cost math still works at first-click-only volume.
- GitHub rate limits become material at real traffic → precompute guides
  for the most-clicked repos, or cache tree listings across generations.
