"""Build the text blob we feed to the embedding model for each repo.

This module is small but disproportionately important: the structure of
the text directly determines what queries can find the repo. See
ADR 0008 (`docs/decisions/0008-source-document-construction.md`) for
the design choices.

Pure functions only — no I/O. Tested in tests/test_document_builder.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional

from .config import ENRICHMENT_DESC_MAX_CHARS, SOURCE_TEXT_MAX_CHARS


@dataclass(frozen=True)
class RepoForEmbedding:
    """Subset of repo fields needed to build the source document.

    Lives here (not in db.py) because the document builder is the only
    consumer and it makes the test surface trivially clean.

    The four enrichment fields (ADR 0020) are empty unless the pipeline
    runs under a "+enrich" label — with them empty the document is
    byte-identical to the pre-enrichment layout, which is what lets
    unchanged repos' vectors be *copied* between labels instead of
    recomputed.
    """
    full_name: str
    description: Optional[str]
    primary_language: Optional[str]
    topics: List[str]
    readme: Optional[str]
    aliases: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    enrichment_description: Optional[str] = None


def build_source_text(repo: RepoForEmbedding) -> str:
    """Construct the text fed to the embedding model for one repo.

    Layout (signal-rich fields first so even aggressive truncation
    preserves them; enrichment sits between metadata and README so it
    always lands inside the model's ~512-token window — manufacturing
    the category vocabulary is the point of the "+enrich" labels):

        {full_name}: {description}
        Language: {primary_language}
        Topics: {topics joined}
        Also known as: {aliases joined}
        Categories: {categories joined}
        Common queries: {queries joined}
        {enrichment description block}

        {readme}

    Missing fields are skipped cleanly. The output is truncated to
    ``SOURCE_TEXT_MAX_CHARS`` to bound network bytes; the model itself
    will truncate again at its tokenizer level.
    """
    parts: List[str] = []

    # Header line: name + description. Almost always present and the
    # single most informative line.
    if repo.description:
        parts.append(f"{repo.full_name}: {repo.description}")
    else:
        parts.append(repo.full_name)

    if repo.primary_language:
        parts.append(f"Language: {repo.primary_language}")

    if repo.topics:
        parts.append(f"Topics: {', '.join(repo.topics)}")

    if repo.aliases:
        parts.append(f"Also known as: {', '.join(repo.aliases)}")

    if repo.categories:
        parts.append(f"Categories: {', '.join(repo.categories)}")

    if repo.queries:
        parts.append(f"Common queries: {'; '.join(repo.queries)}")

    if repo.enrichment_description:
        parts.append(repo.enrichment_description[:ENRICHMENT_DESC_MAX_CHARS])

    # Blank line before README to give the embedding model a clean separator.
    if repo.readme:
        parts.append("")
        parts.append(repo.readme)

    text = "\n".join(parts)

    if len(text) > SOURCE_TEXT_MAX_CHARS:
        text = text[:SOURCE_TEXT_MAX_CHARS]

    return text


def source_hash(text: str) -> str:
    """Stable hash of the source text, for change detection.

    Stored alongside the embedding so we can later identify rows that
    need re-embedding because the underlying repo changed (description
    edited, README updated, etc.) without re-embedding everything.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
