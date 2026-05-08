# Frontend

A single-file HTML/JS UI for the search service.

No build step. No framework. One `index.html` that loads in any
browser and talks to the search service via `fetch()`.

## Run locally

The file works straight off the filesystem if you just open it, but
some browsers (Safari notably) restrict `fetch()` from `file://`
URLs. The reliable way is to serve it over HTTP:

```bash
cd frontend
python -m http.server 3000
# open http://localhost:3000
```

The page assumes the search service is at `http://localhost:8002`.
To point it elsewhere — e.g. a deployed search service — set the
override before the page loads. The simplest way is a tiny tag in
the HTML:

```html
<script>window.GITSEARCH_API_URL = "https://search.example.com";</script>
```

inserted before the existing `<script>` block.

## Deploy

Drop `index.html` on any static host: Vercel, Netlify, Cloudflare
Pages, GitHub Pages, S3+CloudFront. Free tier on any of them is
enough.

If your search service is on a different origin (it almost always
will be in production), set `ALLOWED_ORIGINS` on the search service
to your frontend's deployed origin so the browser permits the
cross-origin POST. The default is `*`, fine for local dev but worth
narrowing in production.

## What the UI does

- Search input + submit (Enter also submits).
- Optional filters: language, min stars, exclude archived.
- Disclosure-toggled "Tune scoring" panel with three sliders for the
  similarity / stars / recency weights. Useful for demoing how the
  hybrid score works — set stars to 0 to see pure semantic search.
- Example chips on first load (clickable, populate the search box
  and submit).
- Each result shows: full name (links to GitHub), description,
  language, star count, last-updated, similarity score, hybrid
  score, top 5 topics.
- Dark mode follows system preference.

## What it deliberately doesn't do

- No pagination beyond 20 results. Top results are what semantic
  search is good at.
- No accounts, no saved searches, no history. Out of scope.
- No build step or `node_modules`. If this UI's needs ever grow
  beyond what one HTML file can hold, port it to Next.js / Astro /
  whatever — the API contract is just `POST /search`, so the port
  is mechanical.
