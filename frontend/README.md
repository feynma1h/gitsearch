# Frontend

The UI for the search service.

No build step, no framework, no dependencies — plain HTML, one
stylesheet, and a handful of ES modules that talk to the search service
via `fetch()`. The visual design is a warm-paper editorial treatment: an
ink-and-cream palette, Newsreader serif for headings and the wordmark,
and deliberately designed wait states for the scale-to-zero cold start.

```
index.html      markup, and the inline snippet that sets the theme
                before first paint
styles.css      the whole design: both palettes, every component
js/
  app.js        entry point — the five phases, the search itself, and
                the wiring from the controls to both
  config.js     API endpoints, the example queries, language colors
  results.js    result cards and the "Why this rank?" breakdown
  guide.js      loading and cancelling a repo's quick-start guide
  markdown.js   the guide's Markdown -> DOM renderer
  session.js    what this visit has learned: guide cache, model names
  theme.js      light/dark toggle
  dom.js        the two helpers everything else builds on
```

## Run locally

Serve it over HTTP:

```bash
cd frontend
python -m http.server 3000
# open http://localhost:3000
```

Opening `index.html` off the filesystem won't work: browsers refuse to
load ES modules from `file://`, and a `file://` page couldn't have
reached the search service anyway — its origin is `null`, which is not
in the service's allow-list.

The page defaults to the deployed search service (`API_BASE` in
[`js/config.js`](js/config.js)). To point it elsewhere — e.g. a local
search service — set the override from a plain `<script>` tag in
`index.html`:

```html
<script>window.GITSEARCH_API_URL = "http://localhost:8002";</script>
```

Anywhere on the page works: module scripts run after the document is
parsed, so an inline tag has always set it first. Note the deployed
service only allows browser requests from the origins in its
`ALLOWED_ORIGINS` env var, so a local page talking to the deployed API
will be blocked by CORS — run a local search service, or test against
your own deployment.

## Deploy

Copy the `frontend/` directory to any static host: Vercel, Netlify,
Cloudflare Pages, GitHub Pages, S3+CloudFront. Free tier on any of them
is enough, and none of them need to build anything — the files ship as
they are. Set `ALLOWED_ORIGINS` on the search service to the frontend's
deployed origin.

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
  score) plus a plain-language explainer. The API also returns a
  fourth, `criticality_contribution`, which the bar ignores because
  its weight ships at 0. If that weight is ever promoted, this needs a
  fourth segment or the bar stops adding up (ADR 0020).
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
  `took_ms` is ~120 ms on a warm cache, which renders as "0.1s" and
  says nothing about the wait the reader actually sat through.
- **The theme is set inline, before first paint.** Everything else
  is a deferred module, but reading `localStorage` and stamping
  `body[data-th]` has to happen before the first frame or the page
  flashes the wrong palette.

## What it deliberately doesn't do

- No pagination beyond 20 results. Top results are what semantic
  search is good at.
- No accounts, no saved searches, no history. Out of scope.
- No build step, no bundler, no `node_modules`. The modules are
  loaded natively by the browser. If this UI's needs ever outgrow
  what plain static files can hold, port it to Next.js / Astro /
  whatever — the API contract is just `POST /search` and
  `GET /guide/{repo_id}`, so the port is mechanical.
