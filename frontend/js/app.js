/* Entry point: the page's five phases, the search itself, and the wiring
 * between the controls and the two of them. Everything that renders a result
 * lives in results.js; everything about a guide lives in guide.js. */
import { $, el } from "./dom.js";
import { SEARCH_URL, EXAMPLES } from "./config.js";
import { noteModel } from "./session.js";
import { initTheme } from "./theme.js";
import { renderHit } from "./results.js";

const els = {
  hero: $("hero"), gap: $("searched-gap"), form: $("search-form"), q: $("q"),
  examples: $("examples"), zeroExamples: $("zero-examples"),
  wait: $("wait"), waitSkeleton: $("wait-skeleton"), waitCold: $("wait-cold"),
  coldTitle: $("cold-title"), coldSub: $("cold-sub"),
  resultsWrap: $("results-wrap"), status: $("status"), results: $("results"),
  zero: $("zero"), error: $("error"), retry: $("retry"),
  tune: $("tune"), tuneToggle: $("tune-toggle"), arch: $("arch"),
  language: $("language"), minStars: $("min-stars"),
  home: $("home"),
};

const state = {
  warmed: false,          // a search has completed this visit (engine is awake)
  coldRetries: 0,         // automatic retries used while the engine wakes
  coldT0: 0,              // start of the current wake, for the narration clock
  lastQuery: "",
  searchSeq: 0,           // invalidates in-flight searches
  timers: [],
};

function after(ms, fn) { state.timers.push(setTimeout(fn, ms)); }
function clearTimers() { state.timers.forEach(clearTimeout); state.timers = []; }

initTheme();

/* ---- Example chips ------------------------------------------------------ */
for (const target of [els.examples, els.zeroExamples]) {
  for (const q of EXAMPLES) {
    const b = el("button", "chip", q);
    b.type = "button";
    b.addEventListener("click", () => { els.q.value = q; doSearch(q); });
    target.appendChild(b);
  }
}

/* ---- Keyboard ----------------------------------------------------------- */
document.addEventListener("keydown", e => {
  const t = document.activeElement && document.activeElement.tagName;
  if (e.key === "/" && t !== "INPUT" && t !== "TEXTAREA" && t !== "SELECT") {
    e.preventDefault(); els.q.focus(); els.q.select();
  }
  if (e.key === "Escape") {
    els.q.value = "";
    if (t === "INPUT") document.activeElement.blur();
  }
});

/* ---- Header ------------------------------------------------------------- */
els.home.addEventListener("click", () => {
  clearTimers(); state.searchSeq++;
  els.q.value = "";
  showPhase("idle");
  setTimeout(() => els.q.focus(), 60);
});

/* ---- Filters / tuning --------------------------------------------------- */
els.tuneToggle.addEventListener("click", () => {
  const open = els.tune.hidden;
  els.tune.hidden = !open;
  els.tuneToggle.setAttribute("aria-expanded", String(open));
});
els.arch.addEventListener("click", () => {
  els.arch.setAttribute("aria-pressed", els.arch.getAttribute("aria-pressed") !== "true");
  researchIfActive();
});
els.language.addEventListener("change", researchIfActive);
els.minStars.addEventListener("change", researchIfActive);
for (const k of ["rel", "pop", "rec"]) {
  const slider = $(`w-${k}`), out = $(`w-${k}-out`);
  slider.addEventListener("input", () => { out.textContent = Number(slider.value).toFixed(1) + "×"; });
  slider.addEventListener("change", researchIfActive);
}
/* Changing a filter or a weight re-runs the search server-side rather than
 * re-ranking what's on screen: a language filter should change which repos
 * come back, not hide 18 of the 20 you can see. */
function researchIfActive() {
  if (state.lastQuery && !els.resultsWrap.hidden || !els.zero.hidden) doSearch(state.lastQuery);
}

/* ---- Phases ------------------------------------------------------------- */
function showPhase(phase) {
  els.hero.hidden = phase !== "idle";
  els.examples.hidden = phase !== "idle";
  els.gap.hidden = phase === "idle";
  els.wait.hidden = phase !== "waiting";
  els.resultsWrap.hidden = phase !== "results";
  els.zero.hidden = phase !== "zero";
  els.error.hidden = phase !== "error";
  if (phase !== "results") { els.results.innerHTML = ""; els.status.textContent = ""; }
  if (phase === "waiting") {
    els.waitSkeleton.hidden = false;
    els.waitCold.hidden = true;
  }
}

const COLD_STAGES = [
  ["Waking up the search engine…",
   "The first search after a quiet spell takes about a minute while the engine starts. After this one, searches take about a second."],
  ["Still waking up — about halfway.",
   "This only happens on the first search of a visit. Yours will run the moment the engine is awake."],
  ["Almost ready…", "Fetching your results now."],
  ["Taking longer than we expected.",
   "Still at it — your search will run automatically the moment the engine responds. Nothing to redo."],
];
// Elapsed ms into the wake at which each stage becomes true. A full cold
// search measures ~60-65s end to end (ADR 0019: container wake + model
// wake + first-query index warmup); "Almost ready" lands where the
// results query actually starts. The last stage is the honesty valve
// for the rare wake that overruns the promise.
const COLD_STAGE_AT = [2500, 30000, 50000, 78000];
function coldStage(i) {
  els.waitSkeleton.hidden = true;
  els.waitCold.hidden = false;
  els.coldTitle.textContent = COLD_STAGES[i][0];
  els.coldSub.textContent = COLD_STAGES[i][1];
}

/* ---- Search ------------------------------------------------------------- */
els.form.addEventListener("submit", e => { e.preventDefault(); doSearch(els.q.value); });
els.q.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); doSearch(els.q.value); }
});
els.retry.addEventListener("click", () => doSearch(state.lastQuery || els.q.value));

async function doSearch(query, isAutoRetry) {
  query = (query || "").trim();
  if (!query) return;
  clearTimers();
  const seq = ++state.searchSeq;
  if (!isAutoRetry) state.coldRetries = 0;
  state.lastQuery = query;
  els.q.value = query;
  showPhase("waiting");

  // The engine sleeps when unused. Narrate the ~60s wake against one
  // persistent clock, so stages track real elapsed time and an automatic
  // retry resumes the story mid-timeline instead of restarting it.
  if (!state.warmed) {
    if (!isAutoRetry || !state.coldT0) state.coldT0 = performance.now();
    const elapsed = performance.now() - state.coldT0;
    let current = -1;
    COLD_STAGE_AT.forEach((at, i) => {
      if (elapsed >= at) current = i;
      else after(at - elapsed, () => seq === state.searchSeq && coldStage(i));
    });
    if (current >= 0) coldStage(current);
  }

  const filters = { exclude_archived: els.arch.getAttribute("aria-pressed") === "true" };
  if (els.language.value !== "Any language") filters.language = els.language.value;
  if (+els.minStars.value > 0) filters.min_stars = +els.minStars.value;
  const weights = {
    similarity: +$("w-rel").value,
    stars: +$("w-pop").value,
    recency: +$("w-rec").value,
  };

  const t0 = performance.now();
  const ctrl = new AbortController();
  after(90000, () => ctrl.abort()); // matches the server's 90s request cap
  try {
    const resp = await fetch(SEARCH_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, limit: 20, filters, weights }),
      signal: ctrl.signal,
    });
    if (seq !== state.searchSeq) return;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (seq !== state.searchSeq) return;
    clearTimers();
    state.warmed = true;
    noteModel("search", data.model);
    if (!data.hits || !data.hits.length) { showPhase("zero"); return; }
    renderResults(data.hits, (performance.now() - t0) / 1000);
  } catch (err) {
    if (seq !== state.searchSeq) return;
    clearTimers();
    // First search of a visit racing the engine's wake-up: retry silently —
    // the wake clock keeps narrating, and a retry is an implementation
    // detail, not news. Only after the retries are spent does the error show.
    if (!state.warmed && state.coldRetries < 3) {
      state.coldRetries += 1;
      after(4000, () => { if (seq === state.searchSeq) doSearch(query, true); });
      return;
    }
    showPhase("error");
  }
}

/* The elapsed time is measured client-side on purpose. The server's took_ms
 * is ~120ms on a warm cache, which renders as "0.1s" and says nothing about
 * the wait the reader actually sat through. */
function renderResults(hits, elapsed) {
  showPhase("results");
  els.status.textContent =
    `${hits.length} result${hits.length === 1 ? "" : "s"} · ${Math.max(elapsed, 0.1).toFixed(1)}s`;
  els.results.innerHTML = "";
  for (const hit of hits) els.results.appendChild(renderHit(hit));
}
