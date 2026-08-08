/* "Show me how to use it" — the per-repo quick-start guide.
 *
 * First generation for a repo takes ~10-20s while the server reads the repo's
 * files, so the wait is narrated rather than spun. After that the guide is
 * cached server-side and in this session, and opens instantly. */
import { el } from "./dom.js";
import { GUIDE_URL } from "./config.js";
import { guideCache, noteModel } from "./session.js";
import { renderGuide } from "./markdown.js";

function setGuideBtn(btn, label, expanded) {
  btn.lastChild.textContent = label;
  btn.setAttribute("aria-label", label);
  btn.setAttribute("aria-expanded", String(expanded));
}

export function toggleGuide(hit, btn, slot) {
  if (slot.firstChild) {                     // open or loading -> close/cancel
    slot.dataset.seq = (+slot.dataset.seq || 0) + 1;   // invalidate in-flight load
    slot.innerHTML = "";
    setGuideBtn(btn, "Show me how to use it", false);
    return;
  }
  const known = guideCache.get(hit.repo_id);
  if (known) {
    slot.appendChild(renderGuide(known.md, known.badge));
    setGuideBtn(btn, "Hide the guide", true);
    return;
  }
  loadGuide(hit, btn, slot);
}

async function loadGuide(hit, btn, slot) {
  const seq = (+slot.dataset.seq || 0) + 1;
  slot.dataset.seq = seq;
  const live = () => +slot.dataset.seq === seq;

  const waiting = el("div", "guide-panel guide-waiting");
  const dot = el("span", "pulse-dot"); dot.setAttribute("aria-hidden", "true");
  const headline = el("p", "headline", `Reading ${hit.full_name}’s files…`);
  waiting.append(dot, headline, el("p", "sub",
    "We’re writing this from the repo’s actual files — 10 to 20 seconds, one time only. After that it opens instantly."));
  slot.appendChild(waiting);
  setGuideBtn(btn, "Reading the repo…", true);

  const stageTimer = setTimeout(() => {
    if (live()) headline.textContent = "Putting the steps together…";
  }, 6500);

  try {
    const resp = await fetch(GUIDE_URL(hit.repo_id));
    if (!live()) return;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (!live()) return;
    const badge = data.cached ? "saved from earlier — opens instantly" : "generated just now";
    guideCache.set(hit.repo_id, { md: data.guide || "", badge });
    noteModel("guide", data.model);
    slot.innerHTML = "";
    slot.appendChild(renderGuide(data.guide || "", badge));
    setGuideBtn(btn, "Hide the guide", true);
  } catch (err) {
    if (!live()) return;
    slot.innerHTML = "";
    const failed = el("div", "guide-panel guide-failed");
    failed.append(
      el("p", "headline", "We couldn’t put the guide together."),
      el("p", "sub", "The repo’s files didn’t load on our side. This usually passes — try once more."));
    const retry = el("button", "retry-btn", "Try again");
    retry.type = "button";
    retry.addEventListener("click", () => { slot.innerHTML = ""; loadGuide(hit, btn, slot); });
    failed.appendChild(retry);
    slot.appendChild(failed);
    setGuideBtn(btn, "Show me how to use it", true);
  } finally {
    clearTimeout(stageTimer);
  }
}
