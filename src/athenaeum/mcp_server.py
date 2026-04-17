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


_MAX_TOP_K = 50
_MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB


def recall_search(
    wiki_root: Path,
    query: str,
    top_k: int = 5,
    *,
    search_backend: str = "keyword",
    cache_dir: Path | None = None,
) -> str:
    """Search the knowledge wiki for pages relevant to *query*.

    Args:
        wiki_root: Path to the wiki directory.
        query: Search query string.
        top_k: Maximum results to return.
        search_backend: ``"keyword"`` (in-memory), ``"fts5"``, or ``"vector"``.
        cache_dir: Directory containing the search index (required for
            fts5/vector backends).

    Returns a formatted string of matching wiki pages with relevance scores
    and content snippets.
    """
    top_k = min(top_k, _MAX_TOP_K)

    if not wiki_root.is_dir():
        return f"Wiki directory not found at {wiki_root}."

    if search_backend in ("fts5", "vector"):
        return _recall_via_backend(
            wiki_root, query, top_k, search_backend, cache_dir
        )

    # Default: in-memory keyword scoring (original behavior)
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


def _recall_via_backend(
    wiki_root: Path,
    query: str,
    top_k: int,
    backend_name: str,
    cache_dir: Path | None,
) -> str:
    """Delegate recall to an indexed search backend, then format results."""
    from athenaeum.search import get_backend

    backend = get_backend(backend_name)
    effective_cache = cache_dir or Path.home() / ".cache" / "athenaeum"

    try:
        hits = backend.query(query, effective_cache, n=top_k)
    except NotImplementedError as exc:
        return str(exc)

    if not hits:
        return f"No wiki pages matched query: {query!r}"

    tokens = _tokenize_query(query)
    parts: list[str] = [f"Found {len(hits)} matching pages:\n"]

    for rank, (filename, name, score) in enumerate(hits, 1):
        page_path = wiki_root / filename
        body = ""
        tags: str | list = "\u2014"
        if page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                name = fm.get("name", name)
                tags = fm.get("tags", "\u2014")
            except (OSError, UnicodeDecodeError):
                pass

        if isinstance(tags, list):
            tags = ", ".join(tags)
        snip = _snippet(body, tokens) if body else ""
        parts.append(
            f"### {rank}. {name} (score: {score:.1f})\n"
            f"**Path:** wiki/{filename}\n"
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
    if len(content.encode("utf-8", errors="replace")) > _MAX_CONTENT_BYTES:
        return f"Error: content exceeds {_MAX_CONTENT_BYTES // (1024 * 1024)} MB limit."

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
    raw_root: Path,
    wiki_root: Path,
    *,
    search_backend: str = "keyword",
    cache_dir: Path | None = None,
) -> "FastMCP":  # noqa: F821 — lazy import
    """Create and return a configured FastMCP server instance.

    Args:
        raw_root: Path to the raw intake directory.
        wiki_root: Path to the compiled wiki directory.
        search_backend: Search backend: ``"keyword"``, ``"fts5"``, or ``"vector"``.
        cache_dir: Directory for search index files (fts5/vector backends).

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

        Dispatches to the configured search backend:

        - ``keyword`` (default fallback): in-memory scoring over frontmatter
          and body; integer-ish relevance scores, higher is better.
        - ``fts5``: SQLite FTS5 over a pre-built index; BM25 scores,
          higher is better.
        - ``vector``: chromadb embeddings over a pre-built index; distance
          scores, lower is better.

        Args:
            query: Search query string (keywords, names, topics — or natural
                language for semantic recall under the vector backend).
            top_k: Maximum number of results to return (default 5).

        Returns:
            Matching wiki pages with relevance scores and content snippets.
        """
        return recall_search(
            wiki_root, query, top_k,
            search_backend=search_backend,
            cache_dir=cache_dir,
        )

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
