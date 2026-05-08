"""Tests for the README client's pure logic.

The HTTP-touching parts of ReadmeClient are integration-tested elsewhere;
here we focus on _finalize() which handles decoding, empty-detection, and
truncation. These cases are easy to get wrong silently (off-by-one on the
truncation cap, mishandling whitespace-only content, etc.) so they're
worth pinning down.
"""

from __future__ import annotations

from src.readme_client import README_MAX_CHARS, ReadmeResult, _finalize


def test_finalize_empty_bytes_is_empty_status():
    assert _finalize(b"").status == "empty"


def test_finalize_whitespace_only_is_empty_status():
    assert _finalize(b"   \n\t  \n").status == "empty"


def test_finalize_normal_text_is_ok():
    result = _finalize(b"# My Project\n\nA library for foo.")
    assert result.status == "ok"
    assert result.content is not None
    assert "My Project" in result.content


def test_finalize_truncates_to_max_chars():
    big = ("a" * (README_MAX_CHARS + 5_000)).encode("utf-8")
    result = _finalize(big)
    assert result.status == "ok"
    assert result.content is not None
    assert len(result.content) == README_MAX_CHARS


def test_finalize_handles_invalid_utf8_gracefully():
    # Lone continuation byte is invalid UTF-8; should not raise.
    result = _finalize(b"hello \x80 world")
    assert result.status == "ok"
    assert result.content is not None
    # The replacement character is U+FFFD.
    assert "\ufffd" in result.content or "hello" in result.content


def test_finalize_strips_surrounding_whitespace():
    result = _finalize(b"\n\n  # Title\n\n  ")
    assert result.status == "ok"
    assert result.content is not None
    assert result.content.startswith("# Title")
    assert not result.content.endswith(" ")


def test_readme_result_is_frozen():
    """ReadmeResult is a frozen dataclass — attempts to mutate must fail."""
    import dataclasses
    r = ReadmeResult(status="ok", content="x")
    try:
        r.status = "error"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ReadmeResult should be frozen")
