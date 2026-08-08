/* The two things this visit learns that more than one module needs.
 *
 * The guide cache keeps a repo's guide in memory once it has been fetched,
 * so closing and reopening it is instant rather than another round trip.
 * The model names arrive on search and guide responses; the footer's
 * "about the search" tooltip starts generic and names them once known. */
import { $ } from "./dom.js";

export const guideCache = new Map();   // repo_id -> { md, badge }

const models = { search: "", guide: "" };

export function noteModel(kind, name) {
  if (!name || models[kind] === name) return;
  models[kind] = name;
  $("about-search").title =
    `Search pairs the ${models.search || "bge-small-en-v1.5"} embedding model ` +
    `with full-text and name matching. ` +
    `Guides are written by ${models.guide || "an AI model"} from each repo’s files.`;
}
