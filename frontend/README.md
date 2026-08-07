# Frontend

A single-file HTML/JS UI for the search service.

No build step. No framework. One `index.html` that loads in any
browser and talks to the search service via `fetch()`. The visual
design is a warm-paper editorial treatment: an ink-and-cream
palette, Newsreader serif for headings and the wordmark, and
deliberately designed wait states for the scale-to-zero cold start.

## Run locally

The file works straight off the filesystem if you just open it, but
some browsers (Safari notably) restrict `fetch()` from `file://`
URLs. The reliable way is to serve it over HTTP:

```bash
cd frontend
python -m http.server 3000
# open http://localhost:3000
```

The page defaults to the deployed search service (the Cloud Run URL
hard-coded near the top of the `<script>` block). To point it
elsewhere — e.g. a local search service — set the override before
the main script runs, with a tiny tag in the HTML:

```html
<script>window.GITSEARCH_API_URL = "http://localhost:8002";</script>
```

inserted anywhere before the existing `<script>` block. Note the
deployed service only allows browser requests from the origins in
its `ALLOWED_ORIGINS` env var, so a local page talking to the
deployed API will be blocked by CORS — run a local search service,
or test against your own deployment.

## Deploy

Drop `index.html` on any static host: Vercel, Netlify, Cloudflare
Pages, GitHub Pages, S3+CloudFront. Free tier on any of them is
enough. Set `ALLOWED_ORIGINS` on the search service to the
frontend's deployed origin.

## What the UI does

- One page, five phases: first visit (hero + example searches),
  waiting, results, no matches, error. The wordmark resets to the
  first-visit view.
- Search box with `/` to focus and `Esc` to clear. Quiet filters:
  language, minimum stars, hide archived. A "Tune ranking" panel
  with three weight sliders (relevance / popularity / recency);
  changing any filter or weight re-runs the current search
  server-side.
- **Honest waits.** Both Cloud Run services scale to zero, so a
  first search after a long idle spell can take about a minute
  (ADR 0019). When one is genuinely slow, the skeleton swaps to a
  staged "waking up the engine" explainer. Warm searches just show
  skeletons.
- Results are cards: avatar (falls back to an initial if GitHub's
  image fails), name, description, language dot, stars, last
  updated, top-5 topics.
- **"Show me how to use it"** — the per-repo quick-start guide, the
  page's signature feature. First generation for a repo takes
  ~10–20 s (the server reads the repo's files) with narrated
  progress; afterwards it's served from cache and opens instantly.
  Rendered as a numbered five-section recipe card with copy buttons
  on code blocks and a "double-check commands" footnote. The
  Markdown renderer is deliberately minimal (headings, fenced code,
  bullets, inline code/links/bold), builds DOM via `textContent`
  (no HTML injection), and drops non-http(s) link targets.
- **"Why this rank?"** — per-result score breakdown: stacked bar of
  the three weight-multiplied components (they sum to the hybrid
  score) plus a plain-language explainer.
- Light and dark themes. Follows the system until the user toggles;
  the explicit choice persists in `localStorage`. Both palettes are
  designed, not inverted.
- Layout-shift discipline (status line has fixed height, skeletons
  match card geometry), `prefers-reduced-motion` support, one
  webfont (Newsreader, headings only).

### Example searches are load-bearing

Every example chip was verified against the live corpus (top-3
results checked for quality). Plausible-sounding queries can return
bad results — "turn markdown into a website" returns converters in
the *reverse* direction — so don't add or reword chips without
re-verifying them against the deployed API.

## Design decisions

- **Filters and weights re-run the search server-side** rather than
  re-ranking the results already on screen. A language filter should
  change *which* repos come back, not hide 18 of the 20 you can see.
- **The search-box placeholder is instructional, not an example.**
  It reads "Describe what you need…". A concrete sample query was
  the obvious alternative, but a placeholder is an implicit
  recommendation, and that phrasing family returns poor results.
- **The status line reports client-measured elapsed time.** Server
  `took_ms` is around 130 ms, which renders as "0.1s" and says
  nothing about the wait the reader actually sat through.

## What it deliberately doesn't do

- No pagination beyond 20 results. Top results are what semantic
  search is good at.
- No accounts, no saved searches, no history. Out of scope.
- No build step or `node_modules`. If this UI's needs ever grow
  beyond what one HTML file can hold, port it to Next.js / Astro /
  whatever — the API contract is just `POST /search` and
  `GET /guide/{repo_id}`, so the port is mechanical.
