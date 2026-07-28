"""Tests for the usage-guide generator (ADRs 0016, 0017).

These pin down the parts that are pure and easy to get subtly wrong: how the
model prompt is assembled from a repo (metadata header, README truncation,
the missing-README fallback), that ``generate_guide`` surfaces failures as
``GuideGenerationError`` rather than leaking the raw exception or an empty
guide, and the mechanics of the agentic tool loop — tools executed and fed
back, browser failures reported as error results instead of aborting, and
the hard budget that forces an answer out of a model that keeps exploring.
The Anthropic call itself is mocked — no network, no key needed.
"""

from __future__ import annotations

import pytest

from service.config import GUIDE_MAX_TOOL_ROUNDS, GUIDE_README_CHAR_LIMIT
from service.db import RepoForGuide
from service.guide import GuideGenerationError, _build_user_message, generate_guide
from service.repo_browser import RepoBrowserError


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


class _ToolUse:
    type = "tool_use"

    def __init__(self, name, input=None, id="toolu_1"):
        self.name = name
        self.input = input or {}
        self.id = id


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def _tool_call(name, input=None, id="toolu_1"):
    return _Message([_ToolUse(name, input, id)], stop_reason="tool_use")


class _Messages:
    """Replays a script: a single result, or a list consumed in order."""

    def __init__(self, script):
        self._script = script if isinstance(script, list) else [script]
        self.calls = []

    async def create(self, **kwargs):
        # Snapshot the messages list: the loop under test mutates one shared
        # list between calls (as the real SDK expects), so storing the
        # reference would make every recorded call show the final state.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        result = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClient:
    def __init__(self, script):
        self.messages = _Messages(script)


# --- Fake repo browser -------------------------------------------------------

class _FakeBrowser:
    def __init__(self, files=None):
        self.files = files if files is not None else {}
        self.calls = []

    async def list_files(self):
        self.calls.append(("list_files", None))
        return "Files in octocat/hello at HEAD:\n" + "\n".join(self.files)

    async def read_file(self, path):
        self.calls.append(("read_file", path))
        if path not in self.files:
            raise RepoBrowserError(f"no file at '{path}' (HEAD)")
        return self.files[path]


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


# --- generate_guide: README-only path (no browser) ---------------------------

async def test_generate_guide_returns_text():
    client = _FakeClient(_Message([_Block("## What it is\nA greeting library.")]))
    guide = await generate_guide(client, _repo())
    assert guide.startswith("## What it is")
    # Model, prompt, and message were actually passed — and no tools:
    # without a browser this must stay the single-call README path.
    (call,) = client.messages.calls
    assert call["model"]
    assert call["system"]
    assert call["messages"][0]["role"] == "user"
    assert "tools" not in call


async def test_generate_guide_raises_on_empty_output():
    client = _FakeClient(_Message([_Block("   ")]))
    with pytest.raises(GuideGenerationError):
        await generate_guide(client, _repo())


async def test_generate_guide_wraps_api_errors():
    client = _FakeClient(RuntimeError("rate limited"))
    with pytest.raises(GuideGenerationError):
        await generate_guide(client, _repo())


# --- generate_guide: agentic path (browser supplied) -------------------------

async def test_agentic_loop_executes_tools_and_returns_guide():
    browser = _FakeBrowser(files={"pyproject.toml": "[project]\nname='hello'"})
    client = _FakeClient([
        _tool_call("list_files", id="toolu_1"),
        _tool_call("read_file", {"path": "pyproject.toml"}, id="toolu_2"),
        _Message([_Block("## What it is\nExplored guide.")]),
    ])

    guide = await generate_guide(client, _repo(), browser)

    assert guide == "## What it is\nExplored guide."
    assert browser.calls == [
        ("list_files", None),
        ("read_file", "pyproject.toml"),
    ]
    first, second, third = client.messages.calls
    # Tools are offered on every call of the loop.
    assert all("tools" in call for call in (first, second, third))
    # Each tool result was fed back, matched to its tool_use id.
    result_1 = second["messages"][-1]["content"][0]
    assert result_1["type"] == "tool_result"
    assert result_1["tool_use_id"] == "toolu_1"
    assert "pyproject.toml" in result_1["content"]
    result_2 = third["messages"][-1]["content"][0]
    assert result_2["tool_use_id"] == "toolu_2"
    assert "[project]" in result_2["content"]


async def test_agentic_tool_failure_becomes_error_result_not_exception():
    browser = _FakeBrowser(files={})  # every read_file raises
    client = _FakeClient([
        _tool_call("read_file", {"path": "missing.md"}),
        _Message([_Block("## What it is\nGuide despite the miss.")]),
    ])

    guide = await generate_guide(client, _repo(), browser)

    assert guide.endswith("despite the miss.")
    result = client.messages.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "missing.md" in result["content"]


async def test_agentic_budget_forces_final_answer():
    browser = _FakeBrowser(files={"a": "x"})
    # The model never stops exploring on its own; after the round budget the
    # loop must force a text answer with tool_choice none.
    script = [_tool_call("read_file", {"path": "a"}) for _ in range(GUIDE_MAX_TOOL_ROUNDS)]
    script.append(_Message([_Block("## What it is\nForced answer.")]))
    client = _FakeClient(script)

    guide = await generate_guide(client, _repo(), browser)

    assert guide.endswith("Forced answer.")
    assert len(client.messages.calls) == GUIDE_MAX_TOOL_ROUNDS + 1
    assert client.messages.calls[-1]["tool_choice"] == {"type": "none"}
    assert all("tool_choice" not in c for c in client.messages.calls[:-1])


async def test_preamble_before_first_heading_is_trimmed():
    # Small models sometimes narrate before the guide; only the fixed
    # five-section body may be cached.
    client = _FakeClient(_Message([
        _Block("Perfect! Let me write the guide now.\n\n## What it is\nA lib."),
    ]))
    guide = await generate_guide(client, _repo())
    assert guide.startswith("## What it is")
    assert "Perfect!" not in guide


async def test_agentic_api_error_wrapped():
    browser = _FakeBrowser()
    client = _FakeClient([
        _tool_call("list_files"),
        RuntimeError("boom"),
        _Message([_Block("unreachable")]),
    ])
    with pytest.raises(GuideGenerationError):
        await generate_guide(client, _repo(), browser)
