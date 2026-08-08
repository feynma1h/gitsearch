/* Result cards, and the "Why this rank?" score breakdown behind each one. */
import { el } from "./dom.js";
import { LANG_COLORS } from "./config.js";
import { toggleGuide } from "./guide.js";

export function renderHit(hit) {
  const li = el("li", "result");
  const owner = hit.full_name.split("/")[0];

  const img = el("img", "avatar");
  img.src = `https://github.com/${encodeURIComponent(owner)}.png?size=80`;
  img.alt = ""; img.loading = "lazy"; img.width = 40; img.height = 40;
  img.addEventListener("error", () => {
    const fb = el("span", "avatar-fallback", owner[0].toUpperCase());
    fb.setAttribute("aria-hidden", "true");
    img.replaceWith(fb);
  });
  li.appendChild(img);

  const body = el("div", "result-body");
  li.appendChild(body);

  const name = el("a", "result-name", hit.full_name);
  name.href = hit.url; name.target = "_blank"; name.rel = "noopener";
  body.appendChild(name);

  if (hit.description) body.appendChild(el("p", "result-desc", hit.description));

  const meta = el("div", "result-meta");
  if (hit.primary_language) {
    const lang = el("span", "lang");
    const dot = el("span", "lang-dot");
    dot.style.background = LANG_COLORS[hit.primary_language] || "var(--ink3)";
    lang.append(dot, hit.primary_language);
    meta.appendChild(lang);
  }
  const stars = el("span", "", `★ ${fmtStars(hit.stars)}`);
  stars.title = "GitHub stars";
  meta.appendChild(stars);
  if (hit.pushed_at) meta.appendChild(el("span", "", fmtUpdated(hit.pushed_at)));
  body.appendChild(meta);

  if (hit.topics && hit.topics.length) {
    const topics = el("div", "topics");
    for (const t of hit.topics.slice(0, 5)) topics.appendChild(el("span", "topic", t));
    body.appendChild(topics);
  }

  const actions = el("div", "result-actions");
  const guideBtn = el("button", "guide-btn");
  guideBtn.type = "button";
  guideBtn.setAttribute("aria-label", "Show me how to use it");
  guideBtn.setAttribute("aria-expanded", "false");
  guideBtn.innerHTML =
    `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3.2C6.8 2.2 5 2 3 2v10.5c2 0 3.8.2 5 1.3 1.2-1.1 3-1.3 5-1.3V2c-2 0-3.8.2-5 1.2Z"/><path d="M8 3.2v10.6"/></svg>`;
  guideBtn.appendChild(el("span", "", "Show me how to use it"));
  const whyBtn = el("button", "why-btn", "Why this rank?");
  whyBtn.type = "button";
  whyBtn.setAttribute("aria-expanded", "false");
  actions.append(guideBtn, whyBtn);
  body.appendChild(actions);

  const scoreSlot = el("div");
  const guideSlot = el("div");
  body.append(scoreSlot, guideSlot);

  whyBtn.addEventListener("click", () => {
    if (scoreSlot.firstChild) {
      scoreSlot.innerHTML = "";
      whyBtn.textContent = "Why this rank?";
      whyBtn.setAttribute("aria-expanded", "false");
    } else {
      scoreSlot.appendChild(renderScore(hit));
      whyBtn.textContent = "Hide ranking";
      whyBtn.setAttribute("aria-expanded", "true");
    }
  });

  guideBtn.addEventListener("click", () => toggleGuide(hit, guideBtn, guideSlot));
  return li;
}

/* The stacked bar shows the three weight-multiplied components, which sum to
 * the hybrid score. The API also returns criticality_contribution, ignored
 * here because its weight ships at 0 — if that weight is ever promoted, this
 * needs a fourth segment or the bar stops adding up (ADR 0020). */
function renderScore(hit) {
  const panel = el("div", "score-panel");
  const rel = hit.similarity_contribution, pop = hit.stars_contribution,
        rec = hit.recency_contribution;
  const total = rel + pop + rec;

  if (total <= 0.0001) {
    panel.appendChild(el("p", "hint",
      "All ranking components are zero for this result — it matched the search but contributes nothing to the ordering."));
    return panel;
  }

  const bar = el("div", "score-bar");
  for (const [v, seg] of [[rel, 1], [pop, 2], [rec, 3]]) {
    const s = el("span");
    s.style.width = (v / total * 100).toFixed(1) + "%";
    s.style.background = `var(--seg${seg})`;
    bar.appendChild(s);
  }
  panel.appendChild(bar);

  const legend = el("div", "score-legend");
  const item = (label, v, seg) => {
    const s = el("span");
    const sw = el("span", "swatch");
    sw.style.background = `var(--seg${seg})`;
    s.append(sw, `${label} ${v.toFixed(2)}`);
    return s;
  };
  legend.append(item("Relevance", rel, 1), item("Popularity", pop, 2), item("Recency", rec, 3));
  legend.appendChild(el("span", "total", `= ${total.toFixed(2)}`));
  panel.appendChild(legend);

  panel.appendChild(el("p", "hint",
    "How well it matches what you typed, how starred it is, and how recently it’s been worked on. Change the mix under “Tune ranking”."));
  return panel;
}

function fmtStars(n) {
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}

function fmtUpdated(iso) {
  const days = Math.floor((Date.now() - Date.parse(iso)) / 864e5);
  if (days <= 0) return "updated today";
  if (days === 1) return "updated yesterday";
  if (days < 30) return `updated ${days} days ago`;
  const d = new Date(iso);
  const mo = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()];
  const yr = d.getFullYear() !== new Date().getFullYear() ? `, ${d.getFullYear()}` : "";
  return `updated ${mo} ${d.getDate()}${yr}`;
}
