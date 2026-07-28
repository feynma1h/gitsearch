# Design sources

## `gitsearch.dc.html` — the 2026-07 frontend treatment

The design file that came back from a Claude Design session for the
frontend redesign (welcoming consumer-tool direction: warm paper
palette, Newsreader serif, designed wait states, the quick-start
guide promoted to signature feature).

It is **not runnable** — it's Claude Design's internal component
format (`x-dc` templates with a `DCLogic` controller, fixture data
loaded from the design session's uploads, and review-only controls
like simulated wait timings). It's kept here as the source of truth
for the visual design.

The production implementation is a hand port:
[`frontend/index.html`](../../frontend/index.html) — same look and
copy, but talking to the real API (`POST /search`,
`GET /guide/{repo_id}`), with real cold-start detection instead of
simulated timings, XSS-safe DOM rendering, and the review palette
removed. Differences from the prototype that were deliberate:

- Filters and ranking weights re-run the search server-side (the
  prototype re-ranked its 20 fixture hits client-side, which would
  mislead — a language filter should change *which* repos return,
  not hide 18 of 20).
- Search-box placeholder is instructional ("Describe what you
  need…") rather than the prototype's example phrase, whose query
  family was verified to return poor results.
- Example chips use the verified welcoming set (see
  `frontend/README.md` — chips are load-bearing and must be
  verified against the live corpus).
- Status line shows client-measured elapsed time (server `took_ms`
  can be ~30 ms, which would render as "0.0s").
