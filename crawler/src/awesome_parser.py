"""Parse awesome-list READMEs into per-repo enrichment entries.

An awesome list is thousands of hours of human curation in one markdown
file: every entry is a repo link, a hand-written one-line description,
and a section heading trail that names the *category* the way a person
searching would ("Frameworks", "Machine Learning", "Static Site
Generators"). That is exactly the vocabulary canonical repos' own
metadata lacks, which is why this parser exists (search v2 phase 2 —
see ADR 0019 and migration 0009).

This module is pure text -> structures, no I/O, mirroring the
document_builder philosophy: the parsing policy is disproportionately
important, so it lives where it can be unit-tested exhaustively.
``mine_awesome.py`` owns fetching, aggregation, and DB writes.

What one entry line yields:

    ### Frameworks                        <- heading trail (categories)
    - [PyTorch](https://github.com/pytorch/pytorch) - Tensors and ...
       ^anchor = alias                       ^rest of line = description

Design choices, each earned on real lists:
  - Only bare ``github.com/owner/repo`` links count (optionally with a
    fragment, ``.git``, or a trailing slash). Deep links (``/blob/``,
    ``/tree/``) usually point at files inside a repo, not the repo as a
    product; precision beats recall here.
  - Only *list-item* and *table-row* lines yield entries. Prose and
    heading lines link repos for many reasons (badges, credits); list
    items are where curation lives.
  - The description is taken from the same line only, never following
    lines — multi-line descriptions exist but are rare, and crossing
    lines risks attributing one repo's text to another.
  - When a line has several repo links, the description goes to the
    first; later links become alias-only entries (a "(fork of [x])"
    remark must not describe x).
  - Heading trails keep every level (h1 included, stripped of the word
    "awesome"), minus list-plumbing headings ("Contents",
    "Contributing", ...): "Awesome Python > Machine Learning" becomes
    ("Python", "Machine Learning").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinedEntry:
    """One (repo link, context) occurrence in one awesome list."""
    full_name: str                 # "owner/repo" exactly as written
    alias: Optional[str]           # anchor text, when it looks like a name
    description: Optional[str]     # cleaned same-line text, when present
    categories: Tuple[str, ...]    # cleaned heading trail, outermost first


# ---------------------------------------------------------------------------
# Regexes (module-level, compiled once)
# ---------------------------------------------------------------------------

# Bare repo links only. The lazy repo group plus the strict tail means
# "github.com/a/b/tree/x" fails to match entirely (the path can't be
# consumed), which is the intended rejection of deep links.
_GITHUB_LINK_RE = re.compile(
    r"\[(?P<anchor>[^\]]*)\]\(\s*"
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9_.-]+?)"
    r"(?:\.git)?/?"
    r"(?:#[^)\s]*)?"
    r"\s*\)"
)

# github.com/<reserved>/... pages that the link regex would misread as
# owner/repo. Lowercase comparison.
_RESERVED_OWNERS = frozenset({
    "about", "apps", "blog", "codespaces", "collections", "contact",
    "customer-stories", "discussions", "enterprise", "events", "explore",
    "features", "issues", "login", "marketplace", "new", "notifications",
    "orgs", "pricing", "pulls", "readme", "resources", "search",
    "security", "settings", "site", "sponsors", "stars", "team", "topics",
    "trending",
})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

# Markdown-stripping pieces, applied in order (images before links —
# an image IS a link syntactically, minus its bang).
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*[^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_CODE_RE = re.compile(r"`([^`]*)`")
_HTML_TAG_RE = re.compile(r"<[^>\n]{1,120}>")
_EMPH_RE = re.compile(r"(\*{1,3}|_{2,3})")
_HEADING_ANCHOR_RE = re.compile(r"\{#[^}]*\}\s*$")
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")

# Trailing `MIT` `Docker` style tag runs (awesome-selfhosted et al.) —
# removed before generic code-tick stripping would keep their words.
_TRAILING_TAGS_RE = re.compile(r"(?:\s*`[^`\n]+`)+\s*$")
# ...and the bracket flavour of the same convention ("desc. [MIT] [website]",
# awesome-cpp et al.), removed after markdown stripping.
_TRAILING_BRACKET_TAGS_RE = re.compile(r"(?:\s*\[[^\]]{1,24}\])+\s*$")
# Leading parenthetical labels ("(label: good first issue) A modern..."),
# from lists that prefix entries with issue-label chips — optionally
# wrapped in italics (single underscores survive the emphasis strip).
_LEADING_PARENS_RE = re.compile(r"^[_*]{0,3}\([^)]{0,60}\)[_*]{0,3}\s*")
# "([Demo](...), [Source Code](...))" style link-parentheticals: cut the
# description at the first "([".
_LINK_PARENS_CUT = " (["

# Headings that structure the *list document*, not the linked repos.
_PLUMBING_HEADINGS = frozenset({
    "about", "acknowledgements", "acknowledgments", "backers",
    "contents", "contributing", "contribution guidelines", "credits",
    "donate", "donations", "faq", "feedback", "footnotes",
    "how to contribute", "licence", "license", "misc", "miscellaneous",
    "other", "other lists", "others", "related", "related lists",
    "resources", "see also", "sponsors", "star history", "support",
    "table of contents", "toc",
})

_MAX_CATEGORY_CHARS = 60
_MAX_DESCRIPTION_CHARS = 400
_MAX_ALIAS_CHARS = 40
_HAS_LETTER_RE = re.compile(r"[A-Za-zÀ-ɏ]")


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------


def _strip_markdown(text: str) -> str:
    """Reduce inline markdown to its visible text."""
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _EMPH_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _clean_heading(raw: str) -> Optional[str]:
    """Heading text -> category label, or None if it's list plumbing.

    Strips markdown/badges/anchors, drops the word "awesome" (an h1 like
    "Awesome Machine Learning" should contribute "Machine Learning"),
    and filters the structural headings every list carries.
    """
    text = _HEADING_ANCHOR_RE.sub("", raw)
    text = _strip_markdown(text)
    text = re.sub(r"\bawesome\b", " ", text, flags=re.IGNORECASE)
    text = _WS_RE.sub(" ", text).strip(" -–—:/·&,")
    if not text or not _HAS_LETTER_RE.search(text):
        return None
    if text.endswith("?"):
        return None          # "Looking for more lists like this?" is prose
    if text.lower() in _PLUMBING_HEADINGS:
        return None
    if len(text) > _MAX_CATEGORY_CHARS:
        return None
    # Category labels feed an English tsvector; any CJK or emoji glues
    # into junk lexemes ("GitHub篇" indexes as neither github nor 篇).
    # Latin-with-diacritics stays below the cutoff and survives.
    if any(ord(ch) >= 0x2E80 for ch in text):
        return None
    return text


def _clean_description(raw: str) -> Optional[str]:
    """Same-line text after a repo link -> description, or None."""
    cut = raw.find(_LINK_PARENS_CUT)
    if cut != -1:
        raw = raw[:cut]
    raw = _TRAILING_TAGS_RE.sub("", raw)
    text = _strip_markdown(raw)
    text = _URL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    # Leading separators between link and description: "- desc",
    # "— desc", ": desc", "| desc" (table cells), "> desc", ") desc"
    # (an image inside the anchor leaves its closing paren behind).
    text = text.strip()
    while text and text[0] in "-–—:·|>,)":
        text = text[1:].lstrip()
    text = _LEADING_PARENS_RE.sub("", text)
    while text and text[0] in "-–—:·|>,)":
        text = text[1:].lstrip()
    text = _TRAILING_BRACKET_TAGS_RE.sub("", text)
    # Keep the first " | "-separated segment: in tables that's the
    # adjacent cell, in prose it's usually a "| CC-BY-4.0" style tail.
    head = text.split(" | ", 1)[0].strip()
    if len(head) >= 8:
        text = head
    text = text.rstrip("|").strip()
    if len(text) > _MAX_DESCRIPTION_CHARS:
        cut_at = text.rfind(" ", 0, _MAX_DESCRIPTION_CHARS)
        text = text[: cut_at if cut_at > 40 else _MAX_DESCRIPTION_CHARS]
        text = text.rstrip() + "…"
    # Below ~8 chars it's a fragment ("list.", "v2."), not a description.
    if len(text) < 8 or not _HAS_LETTER_RE.search(text):
        return None
    return text


def _looks_like_name(text: str) -> bool:
    return (
        0 < len(text) <= _MAX_ALIAS_CHARS
        and len(text.split()) <= 5
        and not text.lower().startswith(("http://", "https://", "www."))
        and bool(_HAS_LETTER_RE.search(text))
    )


def _split_anchor(anchor: str) -> Tuple[Optional[str], Optional[str]]:
    """Anchor text -> (alias, description-carried-in-anchor).

    Most lists put just the name in the anchor. Some (awesome-deep-
    learning's numbered style) pack "Name - Description" into it.
    """
    text = _strip_markdown(anchor)
    if not text:
        return None, None
    if " - " in text:
        left, right = text.split(" - ", 1)
        left, right = left.strip(), right.strip()
        alias = left if _looks_like_name(left) else None
        desc = right if len(right) >= 3 else None
        return alias, desc
    if _looks_like_name(text):
        return text, None
    # A sentence-length anchor is a description in disguise.
    return None, text if len(text) >= 3 else None


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def parse_awesome_readme(
    markdown: str, source_full_name: str = ""
) -> List[MinedEntry]:
    """Extract every repo-link entry from one awesome-list README.

    ``source_full_name`` (the list's own "owner/repo") suppresses
    self-references — every list links itself in badges and headers.
    """
    entries: List[MinedEntry] = []
    trail: Dict[int, str] = {}       # heading level -> cleaned label
    in_fence = False
    source_key = source_full_name.lower()

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            label = _clean_heading(heading.group(2))
            for deeper in list(trail):
                if deeper >= level:
                    trail.pop(deeper)
            if label is not None:
                trail[level] = label
            continue

        if not (_ITEM_RE.match(line) or _TABLE_ROW_RE.match(line)):
            continue

        categories = _current_categories(trail)
        first_with_description = True
        for match in _GITHUB_LINK_RE.finditer(line):
            owner = match.group("owner")
            repo = match.group("repo")
            if owner.lower() in _RESERVED_OWNERS:
                continue
            full_name = f"{owner}/{repo}"
            if full_name.lower() == source_key:
                continue

            alias, anchor_desc = _split_anchor(match.group("anchor"))
            description = None
            if first_with_description:
                description = _clean_description(line[match.end():])
                if description is None:
                    description = (
                        _clean_description(anchor_desc)
                        if anchor_desc else None
                    )
                if description is not None:
                    first_with_description = False

            entries.append(
                MinedEntry(
                    full_name=full_name,
                    alias=alias,
                    description=description,
                    categories=categories,
                )
            )

    return entries


def _current_categories(trail: Dict[int, str]) -> Tuple[str, ...]:
    """The heading trail, outermost first, deduped case-insensitively."""
    seen = set()
    out: List[str] = []
    for level in sorted(trail):
        label = trail[level]
        key = label.lower()
        if key not in seen:
            seen.add(key)
            out.append(label)
    return tuple(out)
