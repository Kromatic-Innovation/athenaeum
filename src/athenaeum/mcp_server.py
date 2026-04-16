"""MCP memory server — read/write gate for an Athenaeum knowledge base.

Tools:
  remember  — append-only write to raw/
  recall    — keyword search over wiki/

Requires the ``mcp`` extra: ``pip install athenaeum[mcp]``
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from athenaeum.models import parse_frontmatter

# ---------------------------------------------------------------------------
# Recall helpers
# ---------------------------------------------------------------------------


def _tokenize_query(query: str) -> list[str]:
    """Split query into lowercase keyword tokens (>=2 chars)."""
    return [t for t in re.split(r"\W+", query.lower()) if len(t) >= 2]


def _score_page(
    tokens: list[str], frontmatter: dict, body: str
) -> float:
    """Score a wiki page against query tokens.

    Frontmatter matches (name, aliases, tags) are weighted 3x vs body matches.
    """
    if not tokens:
        return 0.0

    fm_parts: list[str] = []
    for key in ("name", "aliases", "tags", "description", "title"):
        val = frontmatter.get(key, "")
        if isinstance(val, list):
            fm_parts.append(" ".join(str(v) for v in val))
        else:
            fm_parts.append(str(val))
    fm_text = " ".join(fm_parts).lower()
    body_lower = body.lower()

    score = 0.0
    for token in tokens:
        if token in fm_text:
            score += 3.0
        if token in body_lower:
            score += 1.0
    return score


def _snippet(body: str, tokens: list[str], max_chars: int = 400) -> str:
    """Extract a relevant snippet from body around the first token match."""
    body_lower = body.lower()
    best_pos = len(body)
    for token in tokens:
        pos = body_lower.find(token)
        if 0 <= pos < best_pos:
            best_pos = pos

    if best_pos >= len(body):
        return body[:max_chars].strip() + ("\u2026" if len(body) > max_chars else "")

    start = max(0, best_pos - 80)
    end = min(len(body), start + max_chars)
    prefix = "\u2026" if start > 0 else ""
    suffix = "\u2026" if end < len(body) else ""
    return prefix + body[start:end].strip() + suffix


# ---------------------------------------------------------------------------
# Public API (usable without FastMCP for testing)
# ---------------------------------------------------------------------------


def recall_search(
    wiki_root: Path, query: str, top_k: int = 5
) -> str:
    """Search the knowledge wiki for pages relevant to *query*.

    Returns a formatted string of matching wiki pages with relevance scores
    and content snippets.
    """
    if not wiki_root.is_dir():
        return f"Wiki directory not found at {wiki_root}."

    tokens = _tokenize_query(query)
    if not tokens:
        return "Query too short \u2014 provide at least one keyword (2+ characters)."

    scored: list[tuple[float, Path, dict, str]] = []

    for md_file in wiki_root.rglob("*.md"):
        if md_file.name.startswith("_"):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        fm, body = parse_frontmatter(text)
        score = _score_page(tokens, fm, body)

        if score > 0:
            scored.append((score, md_file, fm, body))

    if not scored:
        return f"No wiki pages matched query: {query!r}"

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    parts: list[str] = [f"Found {len(scored)} matching pages (showing top {len(top)}):\n"]
    for rank, (score, path, fm, body) in enumerate(top, 1):
        name = fm.get("name", path.stem)
        rel_path = path.relative_to(wiki_root)
        tags = fm.get("tags", "\u2014")
        if isinstance(tags, list):
            tags = ", ".join(tags)
        snip = _snippet(body, tokens)
        parts.append(
            f"### {rank}. {name} (score: {score:.1f})\n"
            f"**Path:** wiki/{rel_path}\n"
            f"**Tags:** {tags}\n\n"
            f"{snip}\n"
        )

    return "\n".join(parts)


def remember_write(
    raw_root: Path,
    content: str,
    source: str = "claude-session",
    *,
    wiki_root: Path | None = None,
) -> str:
    """Save a piece of knowledge to the raw intake directory.

    Returns a confirmation message with the file path, or an error string.
    """
    safe_source = "".join(c for c in source if c.isalnum() or c in "-_")
    if not safe_source:
        return "Error: source must contain at least one alphanumeric character."

    target_dir = (raw_root / safe_source).resolve()

    # Guard: must stay inside raw_root, never touch wiki
    if not str(target_dir).startswith(str(raw_root.resolve())):
        return "Error: path traversal detected \u2014 writes are restricted to raw/."
    if wiki_root and str(target_dir).startswith(str(wiki_root.resolve())):
        return "Error: writes to wiki/ are not allowed."

    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_id = uuid.uuid4().hex[:8]
    filename = f"{timestamp}-{short_id}.md"
    filepath = target_dir / filename

    if filepath.exists():
        return f"Error: file already exists at {filepath}. This should not happen."

    filepath.write_text(content, encoding="utf-8")
    return f"Saved to {filepath}"


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------


def create_server(
    raw_root: Path, wiki_root: Path
) -> "FastMCP":  # noqa: F821 — lazy import
    """Create and return a configured FastMCP server instance.

    Requires ``fastmcp`` to be installed (``pip install athenaeum[mcp]``).
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "FastMCP is required for the MCP server. "
            "Install it with: pip install athenaeum[mcp]"
        ) from exc

    mcp = FastMCP(
        "athenaeum",
        instructions=(
            "Knowledge memory server powered by Athenaeum. "
            "Use `remember` to save information to raw intake for later compilation. "
            "Use `recall` to search the compiled wiki for relevant knowledge."
        ),
    )

    @mcp.tool()
    def recall(query: str, top_k: int = 5) -> str:
        """Search the knowledge wiki for pages relevant to a query.

        Uses keyword matching against frontmatter (name, aliases, tags) and
        body text. Frontmatter matches are weighted higher for relevance.

        Args:
            query: Search query string (keywords, names, topics).
            top_k: Maximum number of results to return (default 5).

        Returns:
            Matching wiki pages with relevance scores and content snippets.
        """
        return recall_search(wiki_root, query, top_k)

    @mcp.tool()
    def remember(content: str, source: str = "claude-session") -> str:
        """Save a piece of knowledge to the raw intake directory.

        The content is written as an append-only raw file. It will be compiled
        into the wiki on the next pipeline run.

        Args:
            content: The knowledge to save (markdown string).
            source: Origin label, e.g. "claude-session", "manual".

        Returns:
            Confirmation message with the file path.
        """
        return remember_write(raw_root, content, source, wiki_root=wiki_root)

    return mcp
