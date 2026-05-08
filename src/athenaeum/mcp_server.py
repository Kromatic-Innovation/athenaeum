# SPDX-License-Identifier: Apache-2.0
"""MCP memory server — read/write gate for an Athenaeum knowledge base.

Tools:
  remember  — append-only write to raw/
  recall    — keyword search over wiki/

Requires the ``mcp`` extra: ``pip install athenaeum[mcp]``
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.provenance import validate_field_sources, validate_source_value
from athenaeum.search import score_keyword_page, tokenize_keyword_query

log = logging.getLogger(__name__)

# Default wiki-level source stamped onto remember() writes when the caller
# does not supply ``sources``. ``claude:inferred`` is intentionally
# distinct from any session-id format so downstream provenance audits
# can surface "agent never declared a source" as a first-class signal.
_DEFAULT_INFERRED_SOURCE = "claude:inferred"

# ---------------------------------------------------------------------------
# Recall helpers
# ---------------------------------------------------------------------------

# Back-compat re-exports. The keyword scorer now lives in ``athenaeum.search``
# as a first-class backend alongside FTS5 and vector; these shims keep
# pre-0.2.1 direct callers working without an import churn.
_tokenize_query = tokenize_keyword_query
_score_page = score_keyword_page


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
    extra_roots: list[Path] | None = None,
) -> str:
    """Search the knowledge wiki for pages relevant to *query*.

    Args:
        wiki_root: Path to the wiki directory.
        query: Search query string.
        top_k: Maximum results to return.
        search_backend: ``"keyword"`` (in-memory), ``"fts5"``, or ``"vector"``.
            All three dispatch through ``athenaeum.search.get_backend`` so
            results flow through one code path regardless of backend.
        cache_dir: Directory containing the search index (required for
            fts5/vector backends; ignored by keyword).
        extra_roots: Additional intake roots that were fed into the index
            at build time (e.g. ``raw/auto-memory``). Used here to resolve
            hit filenames of the form ``<root_name>/<relpath>`` back to
            on-disk paths when rendering snippets.

    Returns a formatted string of matching wiki pages with relevance scores
    and content snippets.
    """
    top_k = min(top_k, _MAX_TOP_K)

    if not wiki_root.is_dir():
        return f"Wiki directory not found at {wiki_root}."

    if not tokenize_keyword_query(query):
        return "Query too short \u2014 provide at least one keyword (2+ characters)."

    return _recall_via_backend(
        wiki_root,
        query,
        top_k,
        search_backend,
        cache_dir,
        extra_roots or [],
    )


def _resolve_hit_path(
    filename: str,
    wiki_root: Path,
    extra_roots: list[Path],
) -> tuple[Path | None, str]:
    """Resolve an indexed filename back to an on-disk path + display label.

    Indexed filenames come in two shapes:

    - Wiki entries: bare name (``lean-startup.md``). Resolved against
      ``wiki_root`` with the ``wiki/`` display prefix.
    - Extra-root entries: ``<root_name>/<relpath>``. The first path
      segment is matched against an extra root's ``.name`` and the
      remainder resolved against that root. Display prefix is
      ``<root_name>/`` so the path a human sees matches the indexed
      filename.

    Returns ``(path, display_prefix)``. ``path`` is ``None`` when the
    file cannot be located (stale index, renamed directory); callers
    should render the hit with an empty body rather than crash.
    """
    if "/" not in filename:
        # Wiki entry: flat, shallow.
        return wiki_root / filename, f"wiki/{filename}"

    root_name, _, rel = filename.partition("/")
    for root in extra_roots:
        if root.name == root_name:
            return root / rel, filename
    # Unknown root (index built against a different config). Return the
    # indexed filename verbatim so callers still see what matched rather
    # than a silent empty render.
    return None, filename


def _recall_via_backend(
    wiki_root: Path,
    query: str,
    top_k: int,
    backend_name: str,
    cache_dir: Path | None,
    extra_roots: list[Path],
) -> str:
    """Delegate recall to a registered search backend, then format results."""
    from athenaeum.search import get_backend

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        return str(exc)

    effective_cache = cache_dir or Path.home() / ".cache" / "athenaeum"

    try:
        hits = backend.query(query, effective_cache, n=top_k, wiki_root=wiki_root)
    except NotImplementedError as exc:
        return str(exc)

    if not hits:
        return f"No wiki pages matched query: {query!r}"

    tokens = tokenize_keyword_query(query)
    parts: list[str] = [f"Found {len(hits)} matching pages:\n"]

    for rank, (filename, name, score) in enumerate(hits, 1):
        page_path, display_prefix = _resolve_hit_path(
            filename,
            wiki_root,
            extra_roots,
        )
        body = ""
        tags: str | list = "\u2014"
        if page_path is not None and page_path.is_file():
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
            f"**Path:** {display_prefix}\n"
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
    sources: str | dict | None = None,
) -> str:
    """Save a piece of knowledge to the raw intake directory.

    Args:
        raw_root: Root of the raw intake tree.
        content: Markdown body. May already contain a YAML frontmatter
            block — in that case provenance keys merge into it. If no
            frontmatter is present, one is prepended.
        source: SESSION identifier (legacy parameter name). Used to pick
            the ``raw/<session>/`` subdirectory the file lands in.
            **Not** the per-claim provenance source — see ``sources``.
        wiki_root: Optional wiki root for path-traversal guards.
        sources: Per-claim provenance (issue #90). Either:

            - ``str`` (scalar ``"<type>:<ref>"``) — written as the
              wiki-level ``source`` default.
            - ``dict`` — written as ``field_sources`` (a per-field
              override map). Keys must be strings.
            - ``None`` (default) — stamps ``source: claude:inferred``
              and emits a server-side warning. Untracked-source writes
              are accepted but visible to downstream provenance audits.

    Returns:
        Confirmation message with the file path, or an error string.
    """
    if len(content.encode("utf-8", errors="replace")) > _MAX_CONTENT_BYTES:
        return f"Error: content exceeds {_MAX_CONTENT_BYTES // (1024 * 1024)} MB limit."

    # Validate the per-claim provenance shape early so a malformed
    # ``sources`` argument is rejected before we touch the filesystem.
    try:
        if sources is None:
            wiki_source: str | dict | None = _DEFAULT_INFERRED_SOURCE
            field_sources_map: dict | None = None
            log.warning(
                "remember(): no `sources` supplied; defaulting "
                "wiki-level source to %r. Caller should declare a "
                "source on every write (issue #90).",
                _DEFAULT_INFERRED_SOURCE,
            )
        elif isinstance(sources, str):
            validate_source_value(sources)
            wiki_source = sources
            field_sources_map = None
        elif isinstance(sources, dict):
            # Two shapes accepted: a structured single-source dict
            # ({type, ref, ...}), or a field_sources map ({field: src}).
            # Disambiguate by checking for the SourceRef required keys.
            if {"type", "ref"} <= set(sources.keys()):
                validate_source_value(sources)
                wiki_source = sources
                field_sources_map = None
            else:
                validate_field_sources(sources)
                wiki_source = None
                field_sources_map = sources
        else:
            return f"Error: `sources` must be str, dict, or None; got {type(sources).__name__}"
    except ValueError as exc:
        return f"Error: invalid `sources`: {exc}"

    safe_source = "".join(c for c in source if c.isalnum() or c in "-_")
    if not safe_source:
        return "Error: source must contain at least one alphanumeric character."

    target_dir = (raw_root / safe_source).resolve()
    raw_root_resolved = raw_root.resolve()

    # Guard: must stay inside raw_root, never touch wiki. Use Path.is_relative_to
    # rather than string-prefix compare — str.startswith("/a/raw") matches
    # "/a/raw-sibling" and would accept a traversal that the filesystem sees
    # as a sibling directory, not a descendant.
    if not (
        target_dir == raw_root_resolved or target_dir.is_relative_to(raw_root_resolved)
    ):
        return "Error: path traversal detected \u2014 writes are restricted to raw/."
    if wiki_root:
        wiki_root_resolved = wiki_root.resolve()
        if target_dir == wiki_root_resolved or target_dir.is_relative_to(
            wiki_root_resolved
        ):
            return "Error: writes to wiki/ are not allowed."

    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_id = uuid.uuid4().hex[:8]
    filename = f"{timestamp}-{short_id}.md"
    filepath = target_dir / filename

    if filepath.exists():
        return f"Error: file already exists at {filepath}. This should not happen."

    final_content = _inject_provenance_frontmatter(
        content, wiki_source, field_sources_map
    )
    filepath.write_text(final_content, encoding="utf-8")
    return f"Saved to {filepath}"


def _inject_provenance_frontmatter(
    content: str,
    wiki_source: str | dict | None,
    field_sources_map: dict | None,
) -> str:
    """Stamp ``source`` / ``field_sources`` into the raw file's frontmatter.

    If ``content`` already has a YAML frontmatter block, the provenance
    keys are merged into it (caller-supplied values win on conflict). If
    not, a new frontmatter block is prepended. Either way, the keys land
    at the END of the block so existing key ordering is preserved.

    No-op when both arguments are ``None`` — used for ``sources=None``
    after the default-inferred-source path has supplied ``wiki_source``.
    """
    if wiki_source is None and field_sources_map is None:
        return content

    meta, body = parse_frontmatter(content)
    has_frontmatter = bool(meta)

    if wiki_source is not None:
        meta["source"] = wiki_source
    if field_sources_map is not None:
        meta["field_sources"] = field_sources_map

    if has_frontmatter:
        return render_frontmatter(meta) + body
    # No prior frontmatter — prepend a fresh block. Preserve a blank
    # line between frontmatter and body for readability.
    body_text = content if not content.startswith("\n") else content
    return render_frontmatter(meta) + "\n" + body_text


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------


def create_server(
    raw_root: Path,
    wiki_root: Path,
    *,
    search_backend: str = "keyword",
    cache_dir: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> "FastMCP":  # noqa: F821 — lazy import
    """Create and return a configured FastMCP server instance.

    Args:
        raw_root: Path to the raw intake directory.
        wiki_root: Path to the compiled wiki directory.
        search_backend: Search backend: ``"keyword"``, ``"fts5"``, or ``"vector"``.
        cache_dir: Directory for search index files (fts5/vector backends).
        extra_roots: Additional intake roots that were indexed alongside
            the wiki. Passed through to :func:`recall_search` so raw
            intake hits resolve to their on-disk path.

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
            wiki_root,
            query,
            top_k,
            search_backend=search_backend,
            cache_dir=cache_dir,
            extra_roots=extra_roots,
        )

    @mcp.tool()
    def remember(
        content: str,
        source: str = "claude-session",
        sources: str | dict | None = None,
    ) -> str:
        """Save a piece of knowledge to the raw intake directory.

        The content is written as an append-only raw file. It will be
        compiled into the wiki on the next pipeline run.

        Args:
            content: The knowledge to save (markdown string).
            source: SESSION identifier — selects the ``raw/<session>/``
                subdirectory the file lands in. Examples:
                ``"claude-session"``, ``"manual"``. **Not** a per-claim
                provenance source — pass ``sources`` for that.
            sources: Per-claim provenance (issue #90). Either:

                - scalar ``"<type>:<ref>"`` (e.g.
                  ``"claude:session-2026-05-08"``) — applied as the
                  wiki-level default source for all fields,
                - structured dict ``{type, ref, ts?, confidence?, notes?}``
                  with the same wiki-level effect, or
                - per-field map ``{<field>: <scalar-or-structured>}`` —
                  written as ``field_sources``.

                Omitting ``sources`` defaults to ``source: claude:inferred``
                and logs a server-side warning. Always declare a source.

        Returns:
            Confirmation message with the file path.
        """
        return remember_write(
            raw_root, content, source, wiki_root=wiki_root, sources=sources
        )

    @mcp.tool()
    def list_pending_questions() -> list[dict]:
        """List unanswered pending questions.

        Returns the unanswered blocks from ``wiki/_pending_questions.md`` in
        a shape any agent can render — including containerized agents that
        cannot touch the filesystem directly. Each item has ``id``,
        ``entity``, ``source`` (the originating raw file), ``question``,
        ``conflict_type``, ``description``, and ``created_at``.

        The ``id`` is stable across runs as long as the block's header +
        question text are unchanged, so an agent can call this tool,
        present the list, and then call ``resolve_question`` with the id
        of the chosen item.
        """
        from athenaeum.answers import list_unanswered

        pending_path = wiki_root / "_pending_questions.md"
        return list_unanswered(pending_path)

    @mcp.tool()
    def resolve_question(id: str, answer: str) -> dict:
        """Flip a pending question to answered and write the answer body.

        Locates the block by id, flips ``- [ ]`` -> ``- [x]``, and inserts
        the answer text beneath the checkbox. This is a write to the
        primary file only — archival to ``_pending_questions_archive.md``
        and conversion to a raw intake file both happen on the next
        ``athenaeum ingest-answers`` run (keeping this tool's write path
        small and auditable).

        Args:
            id: The id returned by ``list_pending_questions``.
            answer: The answer body (markdown; may be multi-line).

        Returns:
            A dict with:

            - ``ok`` (bool)
            - ``error_code`` (str | None): one of ``id_not_found``,
              ``already_answered``, ``file_missing``, ``invalid_answer``
              on failure; ``None`` on success.
            - ``message`` (str): human-readable status.
            - ``resolved_block`` (str | None): the rewritten block on
              success; ``None`` on failure.

            For backward compatibility the dict also includes legacy
            aliases ``block`` (= ``resolved_block``) and ``error``
            (= ``message`` on failure). New callers should prefer
            ``error_code`` + ``message`` + ``resolved_block``.
        """
        from athenaeum.answers import resolve_by_id

        result = resolve_by_id(
            pending_path=wiki_root / "_pending_questions.md",
            question_id=id,
            answer=answer,
        )
        # Surface the structured keys explicitly so consumers see them at
        # the top of the dict even when legacy aliases are also present.
        return {
            "ok": result["ok"],
            "error_code": result.get("error_code"),
            "message": result.get("message", ""),
            "resolved_block": result.get("resolved_block"),
            # legacy aliases:
            "block": result.get("block"),
            "error": result.get("error"),
        }

    return mcp
