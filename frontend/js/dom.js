/* The two DOM helpers every module builds on. `el` sets text through
 * textContent, which is why nothing the API returns is ever parsed as
 * HTML — see markdown.js for the rest of that story. */

export const $ = id => document.getElementById(id);

export function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}
