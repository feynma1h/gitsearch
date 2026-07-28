"""Generate the per-repo "how do I use this?" usage guide (ADRs 0016, 0017).

A short, fixed-format step-by-step summary produced by Claude Haiku 4.5.
The five-section shape is identical across repos so the UI can render every
guide the same way. Generation is lazy and cached (see ``db.upsert_guide``);
this module only turns one repo into one guide string.

Two modes, chosen by whether a ``RepoBrowser`` is supplied:

- **Full-repo (ADR 0017).** The model explores the repository through a
  bounded tool loop — ``list_files`` and ``read_file`` backed by the GitHub
  API — so it can pull real install/run commands out of manifests, docs,
  and examples instead of trusting the README to be complete. A GitHub
  failure mid-loop degrades to an error tool result, not a failed guide.
- **README-only (ADR 0016).** One model call over the stored README.
  Used when no browser is available (``GITHUB_TOKEN`` unset).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from .config import (
    GUIDE_MAX_TOKENS,
    GUIDE_MAX_TOOL_ROUNDS,
    GUIDE_MODEL,
    GUIDE_README_CHAR_LIMIT,
)
from .db import RepoForGuide
from .repo_browser import RepoBrowser, RepoBrowserError

logger = logging.getLogger(__name__)


class GuideGenerationError(RuntimeError):
    """Raised when the guide could not be generated (model / network error)."""


# The output format is fixed so the frontend can render it uniformly, and so
# the model can't wander into invented commands: it must ground every step in
# its sources and say plainly when they don't cover a section.
_FORMAT_SPEC = """You write short, practical "how do I use this?" guides for \
GitHub repositories, aimed at a developer who just found the repo in a search \
engine and wants to try it.

Respond in GitHub-flavored Markdown using EXACTLY these five sections, in this \
order, each as a `##` heading:

## What it is
One sentence on what the project does.

## Prerequisites
A short bullet list of what the user needs first (language runtime, package \
manager, accounts, etc.). If none, write "None beyond the basics."

## Install
The command(s) to install or set it up, in a fenced code block, using the \
project's documented method.

## Run it
The command(s) or minimal code to run it or see it working, in a fenced code \
block.

## Next step
One sentence pointing to the most useful next thing (a docs link, a key \
command, or an example)."""

_SYSTEM_PROMPT_README = _FORMAT_SPEC + """

Ground every step in the provided README. Do NOT invent install or run commands \
that the README does not support — if the README doesn't cover a section, say so \
plainly (for example, "The README doesn't document this; see the repository.") \
and continue. Be concise: no preamble, no closing remarks, just the five \
sections."""

_SYSTEM_PROMPT_AGENTIC = _FORMAT_SPEC + """

You may explore the repository before writing: `list_files` shows the file \
tree, `read_file` reads one file. The README is already provided below — read \
only what sharpens the guide beyond it: the package manifest for install and \
prerequisites, a docs or examples file for a real runnable snippet. Typically \
2-4 file reads are enough; stop exploring as soon as you can fill the five \
sections.

Ground every step in the README or a file you actually read. Do NOT invent \
install or run commands they don't support — if a section isn't covered, say \
so plainly (for example, "The repository doesn't document this.") and \
continue. If a tool call fails or your tool budget runs out, write the guide \
from what you have. Be concise: no preamble, no closing remarks, just the \
five sections."""


_TOOLS = [
    {
        "name": "list_files",
        "description": (
            "List the repository's file tree (paths and sizes) at HEAD. "
            "The result never changes, so call it at most once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read one file from the repository at HEAD. Text files only; "
            "large files are truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the repository root, "
                        "e.g. 'docs/quickstart.md'"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


def _build_user_message(repo: RepoForGuide) -> str:
    """Assemble the metadata header + (truncated) README for the model."""
    header = [
        f"Repository: {repo.full_name}",
        f"URL: {repo.url}",
    ]
    if repo.primary_language:
        header.append(f"Primary language: {repo.primary_language}")
    if repo.description:
        header.append(f"Description: {repo.description}")
    if repo.topics:
        header.append(f"Topics: {', '.join(repo.topics)}")

    readme = (repo.readme or "").strip()
    if readme:
        readme = readme[:GUIDE_README_CHAR_LIMIT]
        body = f"README:\n\n{readme}"
    else:
        body = (
            "README: (none available — base the guide on the metadata above and "
            "say plainly where the README doesn't cover a section)"
        )

    return "\n".join(header) + "\n\n" + body


def _text_of(message: Any) -> str:
    """The concatenated text blocks of a response, or raise if empty."""
    text = "".join(
        block.text for block in message.content if block.type == "text"
    ).strip()
    if not text:
        raise GuideGenerationError("model returned no text")
    return text


async def _run_tool(browser: RepoBrowser, block: Any) -> Dict[str, Any]:
    """Execute one tool_use block; failures become error tool results."""
    try:
        if block.name == "list_files":
            content = await browser.list_files()
        elif block.name == "read_file":
            content = await browser.read_file((block.input or {}).get("path", ""))
        else:
            raise RepoBrowserError(f"unknown tool '{block.name}'")
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
        }
    except RepoBrowserError as exc:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(exc),
            "is_error": True,
        }


async def _generate_from_readme(
    client: AsyncAnthropic, repo: RepoForGuide,
) -> str:
    """ADR 0016 path: one call over the stored README."""
    message = await client.messages.create(
        model=GUIDE_MODEL,
        max_tokens=GUIDE_MAX_TOKENS,
        system=_SYSTEM_PROMPT_README,
        messages=[{"role": "user", "content": _build_user_message(repo)}],
    )
    return _text_of(message)


async def _generate_with_exploration(
    client: AsyncAnthropic, repo: RepoForGuide, browser: RepoBrowser,
) -> str:
    """ADR 0017 path: bounded tool loop over the live repository.

    The loop mirrors the standard manual tool-use pattern: call, execute any
    tool_use blocks, feed results back, repeat. After
    ``GUIDE_MAX_TOOL_ROUNDS`` model calls the answer is forced with
    ``tool_choice: none`` so a curious model can't loop forever. Kept as an
    explicit loop (not the SDK's beta tool runner) because the tools close
    over per-request state and the whole thing is ~30 lines.
    """
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _build_user_message(repo)},
    ]

    for _ in range(GUIDE_MAX_TOOL_ROUNDS):
        message = await client.messages.create(
            model=GUIDE_MODEL,
            max_tokens=GUIDE_MAX_TOKENS,
            system=_SYSTEM_PROMPT_AGENTIC,
            messages=messages,
            tools=_TOOLS,
        )
        if message.stop_reason != "tool_use":
            return _text_of(message)

        messages.append({"role": "assistant", "content": message.content})
        results = [
            await _run_tool(browser, block)
            for block in message.content
            if block.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})

    message = await client.messages.create(
        model=GUIDE_MODEL,
        max_tokens=GUIDE_MAX_TOKENS,
        system=_SYSTEM_PROMPT_AGENTIC,
        messages=messages,
        tools=_TOOLS,
        tool_choice={"type": "none"},
    )
    return _text_of(message)


async def generate_guide(
    client: AsyncAnthropic,
    repo: RepoForGuide,
    browser: Optional[RepoBrowser] = None,
) -> str:
    """Generate a usage guide for ``repo``.

    With a ``browser``, the model explores the repository through the tool
    loop; without one, it works from the stored README alone.

    Raises:
        GuideGenerationError: on any model or transport failure, or if the
            model returns no text.
    """
    try:
        if browser is None:
            return await _generate_from_readme(client, repo)
        return await _generate_with_exploration(client, repo, browser)
    except GuideGenerationError:
        raise
    except Exception as exc:  # anthropic API errors, network, timeouts
        raise GuideGenerationError(str(exc)) from exc
