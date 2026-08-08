/* Light/dark toggle. index.html sets body[data-th] inline before first paint
 * so there's no flash; this keeps it tracking the system preference until the
 * user picks a side, after which the choice persists and the system stops
 * mattering. Both palettes are designed, not inverted — see styles.css. */
import { $ } from "./dom.js";

const KEY = "gitsearch-theme";

export function initTheme() {
  const toggle = $("theme");
  let chosen = null;
  try { chosen = localStorage.getItem(KEY); } catch (e) {}

  function setIcon() {
    toggle.textContent = document.body.dataset.th === "dark" ? "☀" : "☾";
  }
  setIcon();

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
    if (!chosen) { document.body.dataset.th = e.matches ? "dark" : "light"; setIcon(); }
  });

  toggle.addEventListener("click", () => {
    const dark = document.body.dataset.th !== "dark";
    document.body.dataset.th = dark ? "dark" : "light";
    chosen = dark ? "dark" : "light";
    try { localStorage.setItem(KEY, chosen); } catch (e) {}
    setIcon();
  });
}
