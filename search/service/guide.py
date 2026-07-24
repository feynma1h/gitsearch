"""Generate the per-repo "how do I use this?" usage guide (ADR 0016).

A short, fixed-format step-by-step summary produced from a repo's README by
Claude Haiku 4.5. The five-section shape is identical across repos so the UI
can render every guide the same way. Generation is lazy and cached (see
``db.upsert_guide``); this module only turns one repo into one guide string.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from .config import GUIDE_MAX_TOKENS, GUIDE_MODEL, GUIDE_README_CHAR_LIMIT
from .db import RepoForGuide

logger = logging.getLogger(__name__)


class GuideGenerationError(RuntimeError):
    """Raised when the guide could not be generated (model / network error)."""


# The output format is fixed so the frontend can render it uniformly, and so
# the model can't wander into invented commands: it must ground every step in
# the README and say plainly when the README doesn't cover a section.
_SYSTEM_PROMPT = """You write short, practical "how do I use this?" guides for \
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
command, or an example).

Ground every step in the provided README. Do NOT invent install or run commands \
that the README does not support — if the README doesn't cover a section, say so \
plainly (for example, "The README doesn't document this; see the repository.") \
and continue. Be concise: no preamble, no closing remarks, just the five \
sections."""


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


async def generate_guide(client: AsyncAnthropic, repo: RepoForGuide) -> str:
    """Generate a usage guide for ``repo``.

    Raises:
        GuideGenerationError: on any model or transport failure, or if the
            model returns no text.
    """
    try:
        message = await client.messages.create(
            model=GUIDE_MODEL,
            max_tokens=GUIDE_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(repo)}],
        )
    except Exception as exc:  # anthropic API errors, network, timeouts
        raise GuideGenerationError(str(exc)) from exc

    text = "".join(
        block.text for block in message.content if block.type == "text"
    ).strip()
    if not text:
        raise GuideGenerationError("model returned no text")
    return text
