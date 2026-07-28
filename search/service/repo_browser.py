"""Read-only GitHub repository browsing for the guide's tool loop (ADR 0017).

Exactly two operations, mirroring the two tools the guide model gets: list
the file tree, read one file. Every output is bounded — entry counts, bytes
downloaded, characters returned — because it goes straight into a model
prompt on a 512Mi instance.

The tree comes from the REST API, which needs a token (unauthenticated
calls are capped at 60/hour per egress IP — useless behind Cloud Run's
shared NAT). File contents come from ``raw.githubusercontent.com``, which
doesn't count against the API quota; the token is deliberately *not* sent
there (public repos don't need it, and credentials shouldn't travel to
hosts that don't require them). Both use ``HEAD`` as the ref, so the
default branch never has to be resolved.
"""

from __future__ import annotations

from typing import List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .config import GUIDE_FILE_CHAR_LIMIT, GUIDE_TREE_MAX_ENTRIES

_TIMEOUT = aiohttp.ClientTimeout(total=10)
# Never pull more than could survive the character cap anyway (4 bytes is
# the widest a UTF-8 character gets); protects memory against huge files.
_MAX_DOWNLOAD_BYTES = GUIDE_FILE_CHAR_LIMIT * 4


class RepoBrowserError(RuntimeError):
    """One browse operation failed. The tool loop reports this to the model
    as an error tool result and continues; it never aborts the guide."""


def _format_tree(
    entries: List[Tuple[str, int]], full_name: str, api_truncated: bool,
) -> str:
    """Render (path, size) pairs as the listing the model sees.

    Oversized trees are cut to ``GUIDE_TREE_MAX_ENTRIES``, preferring
    shallow paths (sorted by depth, then name) so the repo's top-level
    layout always survives the cut — that's what orients the model; the
    deep tail of ``src/`` is what gets dropped.
    """
    total = len(entries)
    truncated = api_truncated or total > GUIDE_TREE_MAX_ENTRIES
    if total > GUIDE_TREE_MAX_ENTRIES:
        entries = sorted(entries, key=lambda e: (e[0].count("/"), e[0]))
        entries = entries[:GUIDE_TREE_MAX_ENTRIES]

    lines = [f"Files in {full_name} at HEAD ({len(entries)} of {total} shown):"]
    if truncated:
        lines.append(
            "(listing truncated — shallow paths kept; deeper files exist "
            "and can still be read by exact path)"
        )
    lines += [f"{path} ({size} B)" for path, size in entries]
    return "\n".join(lines)


def _decode_body(raw: bytes, path: str) -> str:
    """Turn downloaded bytes into prompt-safe text, or raise for binaries."""
    if b"\x00" in raw[:1024]:
        raise RepoBrowserError(f"'{path}' looks binary; not readable as text")
    text = raw.decode("utf-8", errors="replace")
    if len(text) > GUIDE_FILE_CHAR_LIMIT:
        text = (
            text[:GUIDE_FILE_CHAR_LIMIT]
            + f"\n\n[file truncated at {GUIDE_FILE_CHAR_LIMIT} characters]"
        )
    return text


class RepoBrowser:
    """Bounded read-only view of one public GitHub repo at HEAD.

    One instance per guide generation; the aiohttp session is the service's
    shared one. The tree is fetched at most once per instance regardless of
    how often the model calls ``list_files``.
    """

    def __init__(
        self, session: aiohttp.ClientSession, full_name: str, token: str,
    ) -> None:
        self._session = session
        self._full_name = full_name
        self._token = token
        self._listing: Optional[str] = None

    async def list_files(self) -> str:
        """Return the formatted file listing (cached per instance)."""
        if self._listing is not None:
            return self._listing

        url = (
            f"https://api.github.com/repos/{self._full_name}"
            "/git/trees/HEAD?recursive=1"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with self._session.get(
                url, headers=headers, timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 404:
                    raise RepoBrowserError(
                        f"repository {self._full_name} not found on GitHub"
                    )
                if resp.status in (403, 429):
                    raise RepoBrowserError("GitHub API rate limited or denied")
                if resp.status != 200:
                    raise RepoBrowserError(f"GitHub API returned {resp.status}")
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise RepoBrowserError(f"network error talking to GitHub: {exc}")
        except TimeoutError:
            raise RepoBrowserError("GitHub API request timed out")

        entries = [
            (item["path"], int(item.get("size") or 0))
            for item in payload.get("tree", [])
            if item.get("type") == "blob"
        ]
        self._listing = _format_tree(
            entries, self._full_name, bool(payload.get("truncated")),
        )
        return self._listing

    async def read_file(self, path: str) -> str:
        """Return one file's (possibly truncated) text content."""
        path = (path or "").strip().lstrip("/")
        if not path or ".." in path.split("/"):
            raise RepoBrowserError(f"invalid path {path!r}")

        url = (
            f"https://raw.githubusercontent.com/{self._full_name}/HEAD/"
            + quote(path, safe="/")
        )
        try:
            async with self._session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status == 404:
                    raise RepoBrowserError(f"no file at '{path}' (HEAD)")
                if resp.status != 200:
                    raise RepoBrowserError(
                        f"GitHub returned {resp.status} for '{path}'"
                    )
                raw = await resp.content.read(_MAX_DOWNLOAD_BYTES)
        except aiohttp.ClientError as exc:
            raise RepoBrowserError(f"network error talking to GitHub: {exc}")
        except TimeoutError:
            raise RepoBrowserError(f"timed out fetching '{path}'")

        return _decode_body(raw, path)
