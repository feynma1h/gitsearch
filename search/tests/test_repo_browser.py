"""Tests for the guide's repo browser (ADR 0017).

Only the pure parts are tested — tree formatting/truncation and body
decoding — since those hold the bounds that keep tool output prompt-safe.
The fetch layer is a thin aiohttp wrapper exercised end-to-end in staging.
"""

from __future__ import annotations

import pytest

from service.config import GUIDE_FILE_CHAR_LIMIT, GUIDE_TREE_MAX_ENTRIES
from service.repo_browser import RepoBrowserError, _decode_body, _format_tree


# --- _format_tree ------------------------------------------------------------

def test_small_tree_lists_everything_in_api_order():
    entries = [("src/main.rs", 1200), ("README.md", 300)]
    out = _format_tree(entries, "octocat/hello", api_truncated=False)
    assert "2 of 2 shown" in out
    assert "truncated" not in out
    # Under the cap, API order is preserved (no re-sort).
    assert out.index("src/main.rs") < out.index("README.md")


def test_oversized_tree_keeps_shallow_paths_and_notes_truncation():
    # Deep paths first in API order; the cap must still keep the shallow ones.
    entries = [(f"src/deep/nested/file{i}.rs", 10) for i in range(GUIDE_TREE_MAX_ENTRIES)]
    entries += [("Cargo.toml", 500), ("docs/quickstart.md", 800)]
    out = _format_tree(entries, "octocat/hello", api_truncated=False)
    assert f"{GUIDE_TREE_MAX_ENTRIES} of {len(entries)} shown" in out
    assert "truncated" in out
    assert "Cargo.toml" in out
    assert "docs/quickstart.md" in out


def test_api_truncation_flag_alone_still_warns():
    out = _format_tree([("a.txt", 1)], "octocat/hello", api_truncated=True)
    assert "truncated" in out


# --- _decode_body ------------------------------------------------------------

def test_text_body_passes_through():
    assert _decode_body(b"pip install hello\n", "README.md") == "pip install hello\n"


def test_binary_body_is_rejected():
    with pytest.raises(RepoBrowserError, match="binary"):
        _decode_body(b"\x89PNG\x00\x1a", "logo.png")


def test_long_body_is_truncated_with_marker():
    out = _decode_body(b"y" * (GUIDE_FILE_CHAR_LIMIT + 100), "big.txt")
    assert out.count("y") == GUIDE_FILE_CHAR_LIMIT
    assert "truncated" in out
