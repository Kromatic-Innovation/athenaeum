# SPDX-License-Identifier: Apache-2.0
"""MCP memory server — read/write gate for an Athenaeum knowledge base.

Tools:
  remember  — append-only write to raw/
  recall    — keyword search over wiki/

Requires the ``mcp`` extra: ``pip install athenaeum[mcp]``
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from athenaeum.killswitch import is_disabled
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    SOURCE_TYPES,
    is_page_authorized,
    parse_frontmatter,
    render_frontmatter,
    validity_bound_str,
)
from athenaeum.provenance import resolve_remember_extras, resolve_remember_sources
from athenaeum.search import score_keyword_page, tokenize_keyword_query

log = logging.getLogger(__name__)

# Kill switch (issue #379): message + dict returned by the mutating MCP tools
# when athenaeum is disabled at the ``all`` scope. Capture/resolve are the
# "capture" aspect — a ``--compile`` scope leaves them on.
_KILL_SWITCH_MSG = (
    "athenaeum is disabled (kill switch, issue #379): knowledge writes are off. "
    "Run 'athenaeum enable' to restore."
)


def _kill_switch_result() -> dict:
    """Structured refusal for the ``resolve_*`` tools (mirrors their dict shape)."""
    return {
        "ok": False,
        "error_code": "disabled",
        "message": _KILL_SWITCH_MSG,
        "resolved_block": None,
        # legacy aliases (see resolve_question / resolve_merge):
        "block": None,
        "error": _KILL_SWITCH_MSG,
    }

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
    caller_audience: set[str] | None = None,
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
        caller_audience: Read-scope pin for a restricted caller (issue #312).
            ``None`` is the owner / default caller (no filtering). A non-None
            set restricts results to pages the caller is authorized for; the
            predicate is applied inside the backend query (Layer B) AND
            re-checked against fresh on-disk frontmatter at render (Layer C).

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
        caller_audience,
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


# Matches the FIRST ISO-8601 date (YYYY-MM-DD) embedded anywhere in a value.
# ``source_ref`` values are colon-delimited (``api:apollo:2026-05-09``) and
# ``created``/``updated`` may be a full timestamp (``2026-06-30T12:00:00``);
# both cases carry the date as a leading substring of one segment.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_part(value: object) -> str:
    """Return the first ``YYYY-MM-DD`` date found in ``value``, or ``""``.

    Accepts a ``str`` (possibly a colon-delimited ``source_ref`` or an ISO
    timestamp) or a ``date``/``datetime`` (whose ``isoformat`` starts with the
    date). Anything without a recognizable date yields ``""`` so the caller can
    omit the parenthetical rather than render a bare/garbage token.
    """
    if value in (None, ""):
        return ""
    match = _ISO_DATE_RE.search(str(value))
    return match.group(0) if match else ""


def _recall_metadata_lines(fm: dict[str, object]) -> list[str]:
    """Build the compact provenance/context header for one recall hit (#325).

    Returns 0-2 markdown lines inserted between the ``**Tags:**`` line and the
    snippet so a consuming agent can judge trust/currency WITHOUT opening the
    page:

    - Line 1 (``·``-joined): ``**Source:**`` (``source_type`` + the date part
      of ``source_ref``/``created``), ``**Updated:**`` (from ``updated``), and
      ``**Valid:**`` (``<from> → <until>``, ``open`` for a missing bound). Each
      segment is OMITTED at its default — a ``source_type`` of ``inferred`` (or
      absent), an empty ``updated``, and an absent validity window each render
      nothing, so an uncontested/unscoped page adds at most this one line.
    - Line 2 (only when set): ``**Status:**`` pointing at the pending-question
      queue when the page is contradiction-flagged. This is the load-bearing
      case — silently returning one side of a disputed pair is the failure
      this header prevents.

    When none of source/updated/valid/status apply the list is empty and the
    caller renders exactly the pre-#325 output (no blank metadata line).
    """
    segments: list[str] = []

    # Source: only for a non-default, in-vocabulary origin. ``inferred`` (the
    # honest fallback) and absent/typo'd values are treated as default → omit.
    source_type = fm.get("source_type")
    if (
        isinstance(source_type, str)
        and source_type in SOURCE_TYPES
        and source_type != DEFAULT_SOURCE_TYPE
    ):
        date = _date_part(fm.get("source_ref")) or _date_part(fm.get("created"))
        segments.append(
            f"**Source:** {source_type} ({date})"
            if date
            else f"**Source:** {source_type}"
        )

    updated = _date_part(fm.get("updated"))
    if updated:
        segments.append(f"**Updated:** {updated}")

    # Valid: only when the page actually carries a validity window. Reuse the
    # shared bound renderer so the header agrees with the temporal predicates.
    if fm.get("valid_from") or fm.get("valid_until"):
        vfrom = validity_bound_str(fm, "valid_from") or "open"
        vuntil = validity_bound_str(fm, "valid_until") or "open"
        segments.append(f"**Valid:** {vfrom} → {vuntil}")

    lines: list[str] = []
    if segments:
        lines.append(" · ".join(segments))

    status = fm.get("status")
    contested = (isinstance(status, str) and status == "contradiction-flagged") or bool(
        fm.get("contradictions_detected")
    )
    if contested:
        lines.append("**Status:** contradiction-flagged (see _pending_questions.md)")

    return lines


def _recall_via_backend(
    wiki_root: Path,
    query: str,
    top_k: int,
    backend_name: str,
    cache_dir: Path | None,
    extra_roots: list[Path],
    caller_audience: set[str] | None = None,
) -> str:
    """Delegate recall to a registered search backend, then format results."""
    from athenaeum.search import get_backend

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        return str(exc)

    effective_cache = cache_dir or Path.home() / ".cache" / "athenaeum"

    try:
        hits = backend.query(
            query,
            effective_cache,
            n=top_k,
            wiki_root=wiki_root,
            caller_audience=caller_audience,
        )
    except NotImplementedError as exc:
        return str(exc)

    if not hits:
        return f"No wiki pages matched query: {query!r}"

    tokens = tokenize_keyword_query(query)

    # Render each hit, applying Layer C (issue #312): re-check the FRESH
    # on-disk frontmatter for a restricted caller so a stale index (a page
    # whose audience changed since the last rebuild) cannot leak a forbidden
    # page's title, tags, snippet, OR body. Rendered blocks are collected
    # first so the "Found N" header counts only the authorized hits.
    blocks: list[str] = []
    for filename, name, score in hits:
        page_path, display_prefix = _resolve_hit_path(
            filename,
            wiki_root,
            extra_roots,
        )
        body = ""
        tags: str | list = "\u2014"
        fm: dict[str, object] = {}
        readable = False
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                name = fm.get("name", name)
                tags = fm.get("tags", "\u2014")
                readable = True
            except (OSError, UnicodeDecodeError):
                pass

        # Layer C fail-closed: for a restricted caller, drop the hit unless the
        # fresh frontmatter authorizes it. If we couldn't read the file we
        # cannot verify, so withhold. Owner (caller_audience=None) is unaffected.
        if caller_audience is not None:
            if not readable or not is_page_authorized(fm, caller_audience):
                continue

        if isinstance(tags, list):
            tags = ", ".join(tags)
        snip = _snippet(body, tokens) if body else ""
        # Issue #325: compact provenance/context header from the FRESH
        # on-disk frontmatter (same ``fm`` the Layer-C re-read populated).
        # Each field omits at its default, so an uncontested/unscoped page
        # stays nearly as terse as before; a contradiction-flagged page
        # surfaces a Status line pointing at the pending-question queue.
        meta_lines = _recall_metadata_lines(fm)
        meta_block = "".join(f"{line}\n" for line in meta_lines)
        blocks.append(
            f"{name} (score: {score:.1f})\n"
            f"**Path:** {display_prefix}\n"
            f"**Tags:** {tags}\n"
            f"{meta_block}\n"
            f"{snip}\n"
        )

    if not blocks:
        return f"No wiki pages matched query: {query!r}"

    parts: list[str] = [f"Found {len(blocks)} matching pages:\n"]
    for rank, block in enumerate(blocks, 1):
        parts.append(f"### {rank}. {block}")

    return "\n".join(parts)


def remember_write(
    raw_root: Path,
    content: str,
    source: str = "claude-session",
    *,
    wiki_root: Path | None = None,
    sources: str | dict | None = None,
    screening: dict | None = None,
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
        sources: Per-claim provenance (issue #90, design-lock §4 in
            ``docs/provenance-shape.md``). Three accepted shapes:

            1. Scalar ``str`` of form ``"<type>:<ref>"`` (e.g.
               ``"api:apollo:2026-05-09"``) — applied as the wiki-level
               ``source`` default for every field.
            2. ``{"_source": <scalar-or-structured>}`` — wiki-level
               default, structured form preserves
               ``ts``/``confidence``/``notes``. Example::

                   {"_source": {"type": "api", "ref": "apollo:2026-05-09",
                                "confidence": 0.9}}

            3. ``{"_field_sources": {<field>: <source>, ...}}`` — per-field
               attribution. Each value is a scalar or structured source.
               Example::

                   {"_field_sources": {"current_title": "api:apollo:2026-05-09",
                                       "linkedin_url":  "linkedin:alice"}}

            4. Channel-split extras (issue #326) — the dict form may
               include any of these wrapper keys alongside ``_source`` /
               ``_field_sources``, and each is stamped as the matching
               frontmatter key on the raw file:

               * ``_source_type`` → coarse channel classification, one
                 of :data:`athenaeum.models.SOURCE_TYPES`
                 (``user-stated`` / ``agent-observed`` / ``external`` /
                 ``document`` / ``inferred`` / ``model-prior``). Read-side
                 fail-open via ``coerce_source_type``.
               * ``_source_ref`` → ULTIMATE reference (session-id+turn,
                 URL, or document path). NEVER a raw ``auto-memory/...``
                 filename — the read side rejects those.
               * ``_model`` → model-id string for AI-attributed
                 channels (``agent-observed`` / ``inferred`` /
                 ``model-prior``). Optional; when set, downstream
                 audits can trace a stale claim to a specific
                 model cutoff.
               * ``_on_behalf_of`` → W3C PROV ``actedOnBehalfOf``
                 principal name — the responsible human when a model
                 asserted on their behalf.
               * ``_asserter`` → IdP-compatible identity block for
                 ``user-stated`` claims (see
                 ``docs/provenance-shape.md`` §10). Keyed on
                 (``iss``, ``sub``) with a Microsoft Entra
                 (``entra_tid``, ``entra_oid``) branch. ``email`` is
                 display-only — an email change does NOT orphan the
                 identity.

            ``None`` (default) — stamps ``source: claude:inferred`` and
            emits a server-side warning.

            BREAKING (issue #96): the previous bare-dict heuristic that
            inspected ``{type, ref}`` keys is REMOVED. Bare dicts without
            the ``_source`` / ``_field_sources`` wrapper raise
            ``ValueError``. The pathological case (fields literally named
            ``type`` / ``ref``) is now safe via
            ``{"_field_sources": {"type": ..., "ref": ...}}``.

            NOTE: this ``sources`` argument is DIFFERENT from the
            ``sources:`` frontmatter list used by cluster-merge in
            ``athenaeum.merge`` (which is a list of cluster-member uids
            being merged). They share a name for historical reasons; do
            not conflate them.

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
            extras: dict = {}
            log.warning(
                "remember(): no `sources` supplied; defaulting "
                "wiki-level source to %r. Caller should declare a "
                "source on every write (issue #90).",
                _DEFAULT_INFERRED_SOURCE,
            )
        else:
            wiki_source, field_sources_map = resolve_remember_sources(sources)
            extras = resolve_remember_extras(sources)
    except ValueError as exc:
        return f"Error: invalid `sources`: {exc}"
    except TypeError as exc:
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

    # Intake screening (issue #320): classify sensitive content and resolve the
    # read-time `access:` label (#312) to stamp BEFORE the single append-only
    # write below. The screener only inspects `content`; the body bytes are
    # never mutated (label-first, consistent with the write-once raw/ contract).
    try:
        from athenaeum.screening import screen_intake

        screened_access = screen_intake(content, screening)
    except ValueError as exc:
        return f"Error: invalid `screening` config: {exc}"

    final_content = _inject_provenance_frontmatter(
        content, wiki_source, field_sources_map, extras, screened_access=screened_access
    )
    filepath.write_text(final_content, encoding="utf-8")
    return f"Saved to {filepath}"


def _inject_provenance_frontmatter(
    content: str,
    wiki_source: str | dict | None,
    field_sources_map: dict | None,
    extras: dict | None = None,
    screened_access: str | None = None,
) -> str:
    """Stamp ``source`` / ``field_sources`` / channel-split extras into frontmatter.

    If ``content`` already has a YAML frontmatter block, the provenance
    keys are merged into it (caller-supplied values win on conflict). If
    not, a new frontmatter block is prepended. Either way, the keys land
    at the END of the block so existing key ordering is preserved.

    ``extras`` (issue #326) is the channel-split payload keyed for
    frontmatter injection (``source_type`` / ``source_ref`` / ``model``
    / ``on_behalf_of`` / ``asserter``) — the on-disk names the read-side
    parsers (``models.parse_asserter`` etc.) look for.

    No-op when all inputs are absent — used for ``sources=None`` after
    the default-inferred-source path has supplied ``wiki_source``.
    """
    if (
        wiki_source is None
        and field_sources_map is None
        and not extras
        and not screened_access
    ):
        return content

    meta, body = parse_frontmatter(content)
    has_frontmatter = bool(meta)

    if wiki_source is not None:
        meta["source"] = wiki_source
    if field_sources_map is not None:
        meta["field_sources"] = field_sources_map
    if extras:
        for k, v in extras.items():
            meta[k] = v
    if screened_access:
        # Stamp the screener's read-time access label (issue #320). Never
        # downgrade an access the caller already set on the content — take the
        # more restrictive of the two (issue #312 rank).
        from athenaeum.screening import more_restrictive

        existing_access = meta.get("access")
        existing = existing_access if isinstance(existing_access, str) else ""
        meta["access"] = more_restrictive(existing, screened_access)

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
    caller_audience: set[str] | None = None,
    screening: dict | None = None,
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
        caller_audience: Read-scope pin for this server process (issue #312).
            ``None`` (the default) is the owner: the ``recall`` tool returns
            every page, preserving single-user behavior. A non-None role set
            restricts every ``recall`` call to authorized pages only. This is
            pinned HERE by the operator's ``athenaeum serve`` invocation — it
            is deliberately NOT a ``recall()`` tool argument, so a restricted
            agent cannot widen its own scope by passing a different audience.
        screening: Resolved intake-screening config (issue #320) from
            :func:`athenaeum.config.resolve_screening`. ``None`` (default) =
            no screening — every ``remember`` write is unclassified, preserving
            existing behavior. When set, sensitive intake is auto-labeled with
            a read-time ``access:`` level before the append-only write. Pinned
            HERE (not a ``remember()`` tool argument) so a caller cannot
            disable its own screening.

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
            "Use `recall` to search the compiled wiki for relevant knowledge. "
            "Use `list_pending_questions` / `resolve_question` to triage "
            "detector-flagged contradictions, and "
            "`list_pending_merges` / `resolve_merge` to triage resolver-proposed "
            "memory merges (issue #169). "
            "Use `list_pending_decisions` for the unified 'human decisions "
            "needed' queue (questions + merges in one call, issue #401)."
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
            caller_audience=caller_audience,
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
            sources: Per-claim provenance (issue #90, design-lock §4 in
                ``docs/provenance-shape.md``). Three accepted shapes:

                - scalar ``"<type>:<ref>"`` (e.g.
                  ``"api:apollo:2026-05-09"``) — wiki-level default,
                - ``{"_source": <scalar-or-structured>}`` — wiki-level
                  default, structured form preserves
                  ``ts``/``confidence``/``notes``,
                - ``{"_field_sources": {<field>: <source>, ...}}`` —
                  per-field attribution.

                Omitting ``sources`` defaults to ``source: claude:inferred``
                and logs a server-side warning. Always declare a source.

                BREAKING (issue #96): bare dicts without the wrapper keys
                are rejected — see ``remember_write`` for the rationale.

        Returns:
            Confirmation message with the file path.
        """
        if is_disabled("capture", cache_dir=cache_dir):
            return _KILL_SWITCH_MSG
        return remember_write(
            raw_root,
            content,
            source,
            wiki_root=wiki_root,
            sources=sources,
            screening=screening,
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
        if is_disabled("capture", cache_dir=cache_dir):
            return _kill_switch_result()

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

    @mcp.tool()
    def list_pending_merges(full_body: bool = False) -> list[dict]:
        """List unresolved merge proposals (issue #169).

        Returns the unresolved blocks from ``wiki/_pending_merges.md`` —
        resolver-proposed memory merges awaiting human approval. Each
        item has ``id``, ``merge_target_name``, ``sources`` (paths to the
        source memories), ``rationale``, ``draft_merged_body``,
        ``confidence``, and ``created_at``.

        Read-path bound (issue #431, complementing the #400 write-path
        ``max_merge_sources`` suppression): by default ``draft_merged_body``
        is truncated to a bounded preview (env
        ``ATHENAEUM_MERGE_BODY_PREVIEW_CHARS`` > yaml
        ``librarian.merge_body_preview_chars`` > 2000 chars) so a single
        oversized proposal (the withdrawn runaway that prompted this issue
        had a ~878 KB draft body) can't blow out this tool's payload. Each
        item also carries ``draft_merged_body_truncated`` (bool) and
        ``draft_merged_body_full_length`` (the untruncated length) so a
        caller can tell a preview from the real thing.

        Args:
            full_body: Pass ``True`` to skip truncation and get the complete
                ``draft_merged_body`` for every item — use this on demand
                (e.g. right before deciding whether to approve a specific
                merge), not as the default listing call.

        The ``id`` is stable across rationale / draft edits and changes
        only when the source set or target name changes, so an agent can
        call this tool, present the list, and then call ``resolve_merge``
        with the id of the chosen item.
        """
        from athenaeum.config import load_config
        from athenaeum.pending_merges import (
            list_pending_merges as _list_pending_merges,
        )

        merges_path = wiki_root / "_pending_merges.md"
        config = load_config(wiki_root.parent)
        return _list_pending_merges(merges_path, config=config, full_body=full_body)

    @mcp.tool()
    def list_pending_decisions() -> list[dict]:
        """List ALL pending human decisions — questions AND merges (issue #401).

        The unified queue behind ``athenaeum decisions list``. Combines the
        unanswered blocks of ``wiki/_pending_questions.md`` with the
        unresolved blocks of ``wiki/_pending_merges.md`` into one list,
        oldest first, so a containerized agent gets the whole "athenaeum
        needs a human to decide something" backlog in a single call rather
        than having to poll two tools and merge them itself.

        Each item is tagged ``type: "question" | "merge"`` and carries the
        common fields ``id``, ``created_at``, ``summary`` (a one-line,
        answerable question) and ``confidence`` (a float for merges, ``null``
        for questions), plus a type-specific ``payload``. For a merge the
        ``summary`` names each source page by its human title with a one-line
        gist, so the decision is answerable without opening the raw wiki
        files. Resolve items with the existing ``resolve_question`` /
        ``resolve_merge`` tools, dispatching on ``type``.

        Read-path bound (issue #431): a merge item's ``payload["sources"]``
        is capped (env ``ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE`` > yaml
        ``librarian.decisions_max_sources_per_merge`` > 20 entries), with the
        accurate remainder count in ``payload["sources_omitted"]`` — so a
        merge proposal with a very large source list can't blow out this
        tool's payload either.
        """
        from athenaeum.config import load_config, resolve_decisions_max_sources_per_merge
        from athenaeum.decisions import list_pending_decisions as _list_decisions

        config = load_config(wiki_root.parent)
        max_sources_per_merge = resolve_decisions_max_sources_per_merge(config)
        return _list_decisions(wiki_root, max_sources_per_merge=max_sources_per_merge)

    @mcp.tool()
    def list_axiom_audit() -> list[dict]:
        """Axiom assignment audit — every slug's status + promote/demote history (#434).

        ``memory_class: axiom`` must never be minted silently — see
        ``athenaeum axiom promote`` / ``athenaeum axiom demote`` (the
        sanctioned, human-driven authorization surface; this MCP server
        intentionally does not expose a ``promote_axiom`` / ``demote_axiom``
        WRITE tool, so an agent session cannot self-authorize an axiom no
        differently than it can widen its own read scope).

        Returns one entry per distinct slug recorded in
        ``wiki/_axiom_governance.jsonl``, each shaped
        ``{"slug", "active", "history": [...]}`` where ``active`` is
        whether the MOST RECENT action for that slug is a promotion (a
        promote followed by a later demote is inactive; a re-promote after
        that is active again), and ``history`` is the full list of
        promote/demote records (``action``, ``reason``, ``by``, ``at``,
        optional ``scope``) in chronological order — so "when/why/by-whom
        promoted" is fully queryable without leaving the agent session.
        """
        from athenaeum.axiom_governance import list_axiom_audit as _list_axiom_audit

        return _list_axiom_audit(wiki_root)

    @mcp.tool()
    def resolve_merge(id: str, decision: str, note: str = "") -> dict:
        """Approve or reject a pending merge proposal (issue #169, #425).

        Args:
            id: The id returned by ``list_pending_merges``.
            decision: ``"approve"`` dispatches on the proposal's write kind
                (issue #421 classification). A ``create-merged`` proposal
                writes the draft merged body to a fresh ``wiki/<target-
                slug>.md``. A ``fold-into-existing`` proposal writes the
                draft body to the ALREADY-EXISTING canonical page, rewrites
                every inbound ``[[old-slug]]`` wikilink to the canonical
                slug, records the folded-away slugs as ``aliases:`` on the
                canonical page, deletes the old source wiki files, and
                purges their vectors from the search index (when a vector
                backend is configured). Either way, flips the checkbox and
                records a provenance entry naming the sources folded/merged
                in. ``"reject"`` flips the checkbox and writes a
                ``refines:`` declaration into the first source memory so
                the detector's declared-refinement short-circuit
                suppresses the pair on future runs.
            note: Optional human note attached to the decision block.

        Returns:
            A dict with ``ok``, ``error_code``, ``message``,
            ``resolved_block``. A successful ``fold-into-existing``
            approve additionally includes ``folded_sources`` (deleted
            source paths), ``aliases_added``, and ``links_rewritten``.

            For backward compatibility the dict also includes legacy
            aliases ``block`` (= ``resolved_block``) and ``error``
            (= ``message`` on failure), mirroring ``resolve_question``.
            New callers should prefer ``error_code`` + ``message`` +
            ``resolved_block``.
        """
        if is_disabled("capture", cache_dir=cache_dir):
            return _kill_switch_result()

        from athenaeum.pending_merges import resolve_merge as _resolve_merge

        if decision not in ("approve", "reject"):
            return {
                "ok": False,
                "error_code": "invalid_decision",
                "message": (
                    f"decision must be 'approve' or 'reject', got {decision!r}"
                ),
                "resolved_block": None,
                # legacy aliases:
                "block": None,
                "error": (f"decision must be 'approve' or 'reject', got {decision!r}"),
            }
        result = _resolve_merge(
            wiki_root / "_pending_merges.md",
            merge_id=id,
            decision=decision,  # type: ignore[arg-type]
            note=note,
            wiki_root=wiki_root,
            cache_dir=cache_dir,
            search_backend=search_backend,
        )
        response = {
            "ok": result["ok"],
            "error_code": result.get("error_code"),
            "message": result.get("message", ""),
            "resolved_block": result.get("resolved_block"),
            # legacy aliases:
            "block": result.get("resolved_block"),
            "error": (result.get("message", "") if not result.get("ok") else None),
        }
        # Issue #425: present only on a fold-into-existing approve.
        for key in ("folded_sources", "aliases_added", "links_rewritten"):
            if key in result:
                response[key] = result[key]
        return response

    return mcp
