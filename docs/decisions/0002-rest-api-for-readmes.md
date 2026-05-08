# ADR 0002: REST API for README fetching

**Status:** accepted
**Date:** 2026-05-03

## Context

After the metadata crawl, we need to fetch READMEs for downstream semantic
indexing. The GraphQL search endpoint does not return README content; a
separate fetch is required.

## Decision

Use the REST endpoint `GET /repos/{owner}/{repo}/readme`. Each fetch is a
single HTTP request returning base64-encoded content (or a `download_url`
for files >1MB).

## Alternatives considered

- **GraphQL `object(expression: "HEAD:README.md")`.** Allows batching
  ~50 repos per request via aliases, which sounds attractive. Two
  problems: (1) it requires guessing the README filename — `README.md`,
  `README.rst`, `README`, `readme.txt`, etc. — which the REST endpoint
  handles server-side. (2) GraphQL points are shared with the metadata
  crawl's budget; REST has a *separate* 5000 req/hour budget. The
  separation lets the README pass run after (or even alongside) the
  metadata crawl without contention.
- **Cloning each repo and reading from disk.** Massively heavier:
  network, disk, dependency on `git`. No upside for our needs.

## Consequences

- ✅ Filename detection is server-side. No client-side guessing.
- ✅ Uses a separate rate-limit budget from GraphQL.
- ✅ The endpoint distinguishes "no README at all" (404) from "the file
  exists but is empty/whitespace" (200 with empty content). This
  distinction matters downstream — see [ADR 0004](0004-readme-status-enum.md),
  which uses three separate states (`not_found`, `empty`, `error`) so
  the indexer can decide what to do with each. A boolean
  "has-readme/doesn't" would have lost that.
- ✅ Follows redirects for renamed repos. The crawler stores the
  pre-rename `full_name`; the README fetch lands on the new location
  transparently. With GraphQL we'd have had to detect renames in code.
- ⚠️ One request per repo (no batching). This is fine because we are
  rate-limit-bound (5000 req/hr), not request-overhead-bound.
- ⚠️ ~20 hours per token to fetch 100K READMEs. We mitigate by capping
  at top 20K by stars (~4 hours) for now. See [ADR 0003](0003-min-stars-threshold.md)
  for the threshold decision.

## What would change this decision

- Need to fetch >100K READMEs in a single token-hour. Multi-token
  rotation is a more direct fix than switching APIs.
- GitHub deprecates the REST README endpoint (no signal of this).
