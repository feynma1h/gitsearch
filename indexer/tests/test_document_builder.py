"""Tests for the document builder.

This module is pure (no I/O, no async) so testing is straightforward.
The cases are chosen to pin down behaviour that's easy to break silently:
field ordering, optional-field handling, truncation, and hash stability.
"""

from __future__ import annotations

from pipeline.config import SOURCE_TEXT_MAX_CHARS
from pipeline.document_builder import (
    RepoForEmbedding,
    build_source_text,
    source_hash,
)


def _repo(**kwargs) -> RepoForEmbedding:
    """Convenience factory with sensible defaults."""
    defaults = dict(
        full_name="acme/widgets",
        description="A widget library.",
        primary_language="Rust",
        topics=["widgets", "graphics"],
        readme="# Widgets\n\nA library for widgets.",
    )
    defaults.update(kwargs)
    return RepoForEmbedding(**defaults)


def test_full_repo_includes_all_fields():
    text = build_source_text(_repo())
    assert "acme/widgets: A widget library." in text
    assert "Language: Rust" in text
    assert "Topics: widgets, graphics" in text
    assert "# Widgets" in text


def test_signal_rich_fields_come_first():
    """Even after aggressive truncation, the header line should survive."""
    text = build_source_text(_repo())
    first_line = text.split("\n")[0]
    # The header contains the name and description — the strongest signals.
    assert "acme/widgets" in first_line
    assert "A widget library" in first_line


def test_missing_description_falls_back_to_just_name():
    text = build_source_text(_repo(description=None))
    first_line = text.split("\n")[0]
    assert first_line == "acme/widgets"


def test_missing_language_is_skipped_cleanly():
    text = build_source_text(_repo(primary_language=None))
    assert "Language:" not in text
    # And the structure shouldn't have a blank "Language: " line.
    assert "\nLanguage" not in text


def test_empty_topics_are_skipped():
    text = build_source_text(_repo(topics=[]))
    assert "Topics:" not in text


def test_missing_readme_is_skipped():
    text = build_source_text(_repo(readme=None))
    assert "# Widgets" not in text
    # And we don't end with a trailing blank section.
    assert not text.endswith("\n\n")


def test_truncation_to_max_chars():
    big_readme = "x" * (SOURCE_TEXT_MAX_CHARS * 2)
    text = build_source_text(_repo(readme=big_readme))
    assert len(text) == SOURCE_TEXT_MAX_CHARS


def test_truncation_preserves_header():
    """Even when truncated, the metadata header should be intact."""
    big_readme = "x" * (SOURCE_TEXT_MAX_CHARS * 2)
    text = build_source_text(_repo(readme=big_readme))
    assert text.startswith("acme/widgets: A widget library.")


def test_source_hash_is_stable():
    """Same input → same hash. (Otherwise change-detection breaks.)"""
    text = build_source_text(_repo())
    assert source_hash(text) == source_hash(text)


def test_source_hash_changes_with_content():
    """Different input → different hash."""
    h1 = source_hash(build_source_text(_repo(description="A widget library.")))
    h2 = source_hash(build_source_text(_repo(description="A gadget library.")))
    assert h1 != h2


def test_source_hash_is_deterministic_format():
    """The hash should be a hex string, not e.g. raw bytes."""
    h = source_hash("anything")
    assert isinstance(h, str)
    assert all(c in "0123456789abcdef" for c in h)
    assert len(h) == 64  # SHA-256 hex
