"""Tests for the usage-guide generator (ADR 0016).

These pin down the parts that are pure and easy to get subtly wrong: how the
model prompt is assembled from a repo (metadata header, README truncation,
the missing-README fallback), and that ``generate_guide`` surfaces failures
as ``GuideGenerationError`` rather than leaking the raw exception or an empty
guide. The Anthropic call itself is mocked — no network, no key needed.
"""

from __future__ import annotations

import pytest

from service.config import GUIDE_README_CHAR_LIMIT
from service.db import RepoForGuide
from service.guide import GuideGenerationError, _build_user_message, generate_guide


def _repo(readme="Install with pip.", **overrides):
    base = dict(
        repo_id="R_abc",
        full_name="octocat/hello",
        description="A greeting library",
        url="https://github.com/octocat/hello",
        primary_language="Python",
        topics=["cli", "greeting"],
        readme=readme,
        readme_fetched_at=None,
    )
    base.update(overrides)
    return RepoForGuide(**base)


# --- Fake async Anthropic client -------------------------------------------

class _Block:
    def __init__(self, text, type="text"):
        self.type = type
        self.text = text


class _Message:
    def __init__(self, content):
        self.content = content


class _Messages:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeClient:
    def __init__(self, result):
        self.messages = _Messages(result)


# --- _build_user_message ----------------------------------------------------

def test_user_message_includes_metadata_and_readme():
    msg = _build_user_message(_repo(readme="Run `hello --name world`."))
    assert "octocat/hello" in msg
    assert "Primary language: Python" in msg
    assert "Topics: cli, greeting" in msg
    assert "Run `hello --name world`." in msg


def test_user_message_truncates_long_readme():
    long_readme = "x" * (GUIDE_README_CHAR_LIMIT + 5_000)
    msg = _build_user_message(_repo(readme=long_readme))
    # The README body is capped; the header adds a little, so bound loosely.
    assert msg.count("x") == GUIDE_README_CHAR_LIMIT


def test_user_message_handles_missing_readme():
    for empty in (None, "", "   "):
        msg = _build_user_message(_repo(readme=empty))
        assert "none available" in msg


# --- generate_guide ---------------------------------------------------------

async def test_generate_guide_returns_text():
    client = _FakeClient(_Message([_Block("## What it is\nA greeting library.")]))
    guide = await generate_guide(client, _repo())
    assert guide.startswith("## What it is")
    # Model, prompt, and message were actually passed.
    (call,) = client.messages.calls
    assert call["model"]
    assert call["system"]
    assert call["messages"][0]["role"] == "user"


async def test_generate_guide_raises_on_empty_output():
    client = _FakeClient(_Message([_Block("   ")]))
    with pytest.raises(GuideGenerationError):
        await generate_guide(client, _repo())


async def test_generate_guide_wraps_api_errors():
    client = _FakeClient(RuntimeError("rate limited"))
    with pytest.raises(GuideGenerationError):
        await generate_guide(client, _repo())
