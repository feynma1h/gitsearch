/* Render the guide's fixed five-section Markdown (## headings, fenced code,
 * "- " bullets, inline `code` / [links] / **bold**) into DOM nodes. Built
 * with textContent throughout, so nothing from the guide is parsed as HTML;
 * link targets are dropped unless they're http(s).
 *
 * This is deliberately not a general Markdown parser. It handles exactly what
 * the guide prompt produces, which is what keeps it small enough to audit. */
import { el } from "./dom.js";

export function renderGuide(md, badgeText) {
  const panel = el("div", "guide-panel guide-open");
  const head = el("div", "guide-head");
  head.append(el("span", "guide-eyebrow", "Quick-start guide"), el("span", "guide-badge", badgeText));
  panel.appendChild(head);

  const sections = md.replace(/\r\n/g, "\n").split(/^## /m).filter(s => s.trim());
  sections.forEach((sec, i) => {
    const nl = sec.indexOf("\n");
    const title = (nl === -1 ? sec : sec.slice(0, nl)).trim();
    const lines = nl === -1 ? [] : sec.slice(nl + 1).split("\n");

    const section = el("div", "guide-section");
    const shead = el("div", "guide-section-head");
    shead.append(
      el("span", "guide-section-num", String(i + 1).padStart(2, "0")),
      el("span", "guide-section-title", title));
    section.appendChild(shead);

    let j = 0;
    while (j < lines.length) {
      const line = lines[j];
      if (line.startsWith("```")) {
        const buf = [];
        j++;
        while (j < lines.length && !lines[j].startsWith("```")) buf.push(lines[j++]);
        j++;
        section.appendChild(renderCodeBlock(buf.join("\n")));
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        const ul = el("ul");
        while (j < lines.length && /^\s*[-*]\s+/.test(lines[j])) {
          const li = el("li");
          appendInline(li, lines[j].replace(/^\s*[-*]\s+/, ""));
          ul.appendChild(li);
          j++;
        }
        section.appendChild(ul);
        continue;
      }
      if (!line.trim()) { j++; continue; }
      const para = [];
      while (j < lines.length && lines[j].trim() &&
             !lines[j].startsWith("```") && !/^\s*[-*]\s+/.test(lines[j])) {
        para.push(lines[j]); j++;
      }
      const p = el("p");
      appendInline(p, para.join(" "));
      section.appendChild(p);
    }
    panel.appendChild(section);
  });

  panel.appendChild(el("p", "guide-footnote",
    "Generated from this repo’s files — double-check commands before running."));
  return panel;
}

function renderCodeBlock(text) {
  const wrap = el("div", "codeblock");
  const pre = el("pre");
  pre.appendChild(el("code", "", text));
  const copy = el("button", "copy-btn", "Copy");
  copy.type = "button";
  copy.addEventListener("click", () => {
    try { navigator.clipboard.writeText(text); } catch (e) {}
    copy.textContent = "Copied";
    setTimeout(() => { copy.textContent = "Copy"; }, 1600);
  });
  wrap.append(pre, copy);
  return wrap;
}

function appendInline(node, text) {
  const rx = /(`[^`]+`)|\[([^\]]+)\]\(([^)]+)\)|(\*\*[^*]+\*\*)/;
  let rest = text;
  while (rest) {
    const m = rest.match(rx);
    if (!m) { node.append(rest); break; }
    if (m.index > 0) node.append(rest.slice(0, m.index));
    const s = m[0];
    if (s[0] === "`") {
      node.appendChild(el("code", "", s.slice(1, -1)));
    } else if (s[0] === "[") {
      if (/^https?:\/\//i.test(m[3])) {
        const a = el("a", "", m[2]);
        a.href = m[3]; a.target = "_blank"; a.rel = "noopener";
        node.appendChild(a);
      } else {
        node.append(m[2]);
      }
    } else {
      node.appendChild(el("strong", "", s.slice(2, -2)));
    }
    rest = rest.slice(m.index + s.length);
  }
}
