/* Where the API lives, and the two constant tables the UI reads from.
 *
 * API_BASE is the deployed search service. Override it by setting
 * window.GITSEARCH_API_URL from a plain <script> tag in index.html: module
 * scripts run after the document is parsed, so an inline tag anywhere on the
 * page has already set it by the time this module evaluates (see README). */
export const API_BASE =
  window.GITSEARCH_API_URL ||
  "https://gitsearch-search-148185858207.asia-southeast1.run.app";
export const SEARCH_URL = API_BASE + "/search";
export const GUIDE_URL = repoId => `${API_BASE}/guide/${encodeURIComponent(repoId)}`;

/* Example queries. Every one of these was verified against the live corpus
 * (top-3 results checked). Don't add new ones without doing the same —
 * friendly-sounding queries can return bad results. */
export const EXAMPLES = [
  "download videos from youtube",
  "remove the background from an image",
  "recipe manager",
  "home automation",
  "web scraping in python",
  "fast http server in rust",
];

/* Dot color per language, for the languages the filter offers. */
export const LANG_COLORS = {
  Rust:"#DEA584", Go:"#4FA8C7", C:"#8C8C8C", "C++":"#F34B7D", "C#":"#178600",
  Python:"#4B8BBE", JavaScript:"#C9A227", TypeScript:"#3178C6", Shell:"#89A855",
  Lua:"#5B7FC7", Zig:"#EC915C", Java:"#B07219", Kotlin:"#A97BFF", PHP:"#4F5D95",
  Ruby:"#701516", Swift:"#F05138",
};
