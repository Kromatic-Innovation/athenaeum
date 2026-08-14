# SPDX-License-Identifier: Apache-2.0
"""MCP memory server — read/write gate for an Athenaeum knowledge base.

Registers 12 tools (issue athenaeum#538 — the count was previously under-reported as 2;
issue athenaeum#864 added ``read_person``):

  Reads:  recall, list_pending_questions, list_pending_merges,
          list_pending_decisions, list_axiom_audit, scan_retraction_cascade,
          calibration_summary, read_person
  Writes: remember, resolve_question, resolve_merge, review_audit_item

Audience scoping (issue athenaeum#312, athenaeum#538). ``caller_audience`` is pinned ONCE at
``create_server`` time (never a per-tool argument, so a restricted agent cannot
widen its own scope) and governs the whole process:

  - ``recall`` and every page-content-bearing LIST/READ tool
    (``list_pending_questions`` / ``list_pending_merges`` /
    ``list_pending_decisions`` / ``read_person``) apply the SAME fail-closed
    read predicate — a restricted caller sees only pending items (or a
    person page) whose source pages they are authorized to read, so no tool
    returns page content ``recall`` would withhold. ``read_person``
    additionally never returns a contact value for a page it withholds
    (issue athenaeum#864).
  - The three human-decision-queue mutators (``resolve_question`` /
    ``resolve_merge`` / ``review_audit_item``) fail closed for any restricted
    (non-owner) caller — adjudicating the operator's contradiction/merge queue
    is an owner-only action.
  - ``remember`` is intentionally NOT audience-scoped: intake is write-only and
    compiles through the normal read-time screening path (issue athenaeum#320). See
    ``docs/security-posture.md``.

Requires the ``mcp`` extra: ``pip install athenaeum[mcp]``
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.config import resolve_cache_dir
from athenaeum.killswitch import is_disabled
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    SOURCE_TYPES,
    EntityIndex,
    is_page_authorized,
    parse_frontmatter,
    render_frontmatter,
    validity_bound_str,
)
from athenaeum.provenance import resolve_remember_extras, resolve_remember_sources
from athenaeum.search import score_keyword_page, tokenize_keyword_query
from athenaeum.storage import (
    is_excluded,
    is_recallable,
    storage_policy_configured,
    write_raw_intake,
)

if TYPE_CHECKING:
    # Issue athenaeum#550: resolves the forward reference on ``create_server``'s return
    # annotation for mypy WITHOUT importing fastmcp at runtime — the real
    # import stays lazy (inside create_server) so athenaeum works without the
    # optional ``mcp`` extra installed.
    from fastmcp import FastMCP

    # Issue athenaeum#885: annotation-only. The `pii` import stays lazy at every
    # call site in this module (the existing convention here), so this never
    # pulls the module in at import time.
    from athenaeum.pii import ExcludedRecordIndex

log = logging.getLogger(__name__)

# Kill switch (issue athenaeum#379): message + dict returned by the mutating MCP tools
# when athenaeum is disabled at the ``all`` scope. Capture/resolve are the
# "capture" aspect — a ``--compile`` scope leaves them on.
_KILL_SWITCH_MSG = (
    "athenaeum is disabled (kill switch, issue athenaeum#379): knowledge writes are off. "
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


# Audience write-guard (issue athenaeum#538): the three human-decision-queue mutators
# (resolve_question / resolve_merge / review_audit_item) adjudicate the
# operator's contradiction/merge queue and are OWNER-ONLY. A restricted
# (non-None caller_audience) process is refused fail-closed. ``remember`` is
# deliberately NOT guarded here — intake is write-only and screened downstream.
_FORBIDDEN_MSG = (
    "athenaeum: this decision-queue action is owner-only and is not available "
    "to a restricted caller_audience (issue athenaeum#538)."
)


def _forbidden_result() -> dict:
    """Structured fail-closed refusal for the ``resolve_*`` tools (dict shape)."""
    return {
        "ok": False,
        "error_code": "forbidden",
        "message": _FORBIDDEN_MSG,
        "resolved_block": None,
        # legacy aliases (see resolve_question / resolve_merge):
        "block": None,
        "error": _FORBIDDEN_MSG,
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
    config: dict[str, object] | None = None,
    with_pii: bool = False,
    usage_classes: Collection[str] | None = None,
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
        caller_audience: Read-scope pin for a restricted caller (issue athenaeum#312).
            ``None`` is the owner / default caller (no filtering). A non-None
            set restricts results to pages the caller is authorized for; the
            predicate is applied inside the backend query (Layer B) AND
            re-checked against fresh on-disk frontmatter at render (Layer C).
        config: Resolved ``athenaeum.yaml`` config (issue athenaeum#532). Used to honor
            the storage-adapter ``recallable`` corpus policy: a hit whose entity
            class routes to a surface with ``recallable: false`` is dropped at
            the render layer (Layer C), the same fresh-frontmatter re-check the
            audience predicate uses. ``None`` (default) skips the check —
            today's behavior — and is a no-op for the default configuration
            (every class maps to the all-true wiki surface).
        with_pii: Resolve each surviving hit's EXCLUDED fields and render them
            with the hit (issue athenaeum#885). Default ``False``, and the default
            path is byte-identical and free: with the flag unset, ZERO
            excluded-surface scans are performed and the output is exactly what
            it was before this parameter existed.

            This is strictly a RENDER-layer join, never a search-time
            predicate. It cannot widen the candidate set and cannot make an
            excluded page become a hit — excluded values are never indexed and
            are not searchable, only resolvable on a hit the corpus already
            produced. It is applied AFTER the fail-closed audience drop and
            AFTER the athenaeum#532 ``recallable`` drop, so a hit either of those
            removes never triggers an excluded-surface lookup at all and a
            restricted caller cannot use the flag to probe whether a record
            exists for a page it may not read.

            Authorization is deliberately unchanged: the rule is identical to
            :func:`person_read`'s — the audience check decides whether the hit
            exists at all, and for a hit that survives it the flag yields
            values. Who may SET the flag remains the deferred athenaeum#864
            question; this parameter neither widens nor narrows it.
        usage_classes: Restrict resolved excluded values to these usage classes
            (issue athenaeum#866), threaded to the join exactly as
            :func:`athenaeum.pii.read_entity` accepts it. ``None`` (default)
            returns every value. Only meaningful with *with_pii* — the flag is
            not usage-class-blind, so a caller that must not receive a
            provider-sourced address (``docs/security-posture.md`` §2.3) can
            filter it here the same way ``read_person``'s callers already can.

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
        config,
        with_pii=with_pii,
        usage_classes=usage_classes,
    )


def person_read(
    knowledge_root: Path,
    uid: str,
    *,
    include_contact_data: bool = False,
    usage_classes: Collection[str] | None = None,
    caller_audience: set[str] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Read one person's page by uid, with contact-data inclusion gated by a flag.

    The MCP-facing wrapper around :func:`athenaeum.pii.read_person` — the ONE
    sanctioned way this server reads a person's contact data (issue athenaeum#864).
    Applies the SAME fail-closed audience predicate ``recall`` applies
    (:func:`athenaeum.models.is_page_authorized`, re-checked against fresh
    on-disk frontmatter) BEFORE assembling any contact data, so a restricted
    ``caller_audience`` can never obtain via this function what ``recall``
    would withhold for the same page — and never receives a contact value at
    all for a page it is not authorized to read.

    Args:
        knowledge_root: Root of the knowledge base (parent of ``wiki/``).
        uid: The person's durable uid.
        include_contact_data: When ``True``, the actual contact values
            (:data:`athenaeum.pii.CONTACT_DATA_FIELDS`) are included, read
            from the surface :func:`athenaeum.pii.contacts_surface_root`
            resolves — this function never constructs that path itself,
            ``pii.read_person`` does. Default ``False``: each withheld field
            carries a redaction marker instead of its value.
        usage_classes: Restrict returned contact values to these usage classes
            (issue athenaeum#866) — e.g.
            :data:`athenaeum.pii.OUTREACH_ELIGIBLE_CLASSES` for a caller that
            must not receive a provider-sourced address. ``None`` returns every
            value, each carrying its classification in the result's
            ``classifications``. Independent of ``caller_audience``: that gates
            whether the caller may read the PAGE, this gates which KIND of
            contact value the answer may contain.
        caller_audience: Read-scope pin (issue athenaeum#312/#538). ``None`` is the
            owner (no check). A non-None set is checked fail-closed against
            the resolved page's frontmatter.
        config: Resolved ``athenaeum.yaml`` config, threaded to
            :func:`athenaeum.pii.read_person` so the contact surface resolves
            per the operator's ``storage.mapping``.

    Returns:
        A JSON string: the module's fail-closed refusal shape
        (:func:`_forbidden_result`) for an unauthorized restricted caller, a
        ``{"ok": False, "error": ...}`` message for an unknown uid, or
        :meth:`athenaeum.pii.PersonRead.to_dict`.
    """
    from athenaeum import pii

    wiki_root = knowledge_root / "wiki"
    page_path = EntityIndex(wiki_root).get_by_uid(uid)
    if page_path is None:
        return json.dumps(
            {"ok": False, "error": f"person not found: uid={uid!r}"}, indent=2
        )

    # Fail-closed audience check BEFORE any contact data is assembled (issue
    # athenaeum#864's "must NEVER receive contact values" requirement) — mirrors
    # `_recall_via_backend`'s Layer C re-check against fresh on-disk
    # frontmatter rather than trusting a cached/stale value.
    if caller_audience is not None:
        try:
            text = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return json.dumps(_forbidden_result(), indent=2)
        meta, _ = parse_frontmatter(text)
        if not is_page_authorized(meta, caller_audience):
            return json.dumps(_forbidden_result(), indent=2)

    result = pii.read_person(
        knowledge_root,
        config,
        uid,
        include_contact=include_contact_data,
        usage_classes=usage_classes,
    )
    if result is None:
        return json.dumps(
            {"ok": False, "error": f"person not found: uid={uid!r}"}, indent=2
        )
    return json.dumps(result.to_dict(), indent=2)


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
    """Build the compact provenance/context header for one recall hit (athenaeum#325).

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
    caller renders exactly the pre-athenaeum#325 output (no blank metadata line).
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


def _excluded_block_for_hit(
    page_path: Path | None,
    fm: dict[str, object],
    *,
    wiki_root: Path,
    config: dict[str, object] | None,
    indexes: dict[str, ExcludedRecordIndex],
    usage_classes: Collection[str] | None,
) -> str:
    """Render one hit's excluded fields (or their redaction markers), or ``""``.

    The ``with_pii`` join (issue athenaeum#885). Called ONLY for a hit that has
    already survived both Layer-C drops, and only when the caller asked for it.

    It reuses athenaeum#883's public, ``EntityIndex``-free assembly seam
    (:func:`athenaeum.pii.assemble_excluded_read`) and never
    ``read_entity``/``read_entities``: this function already HOLDS the hit's
    fresh frontmatter from the Layer-C re-read, so building an ``EntityIndex``
    to reach the same logic would be a defect, not an optimization detail —
    25.2s of the measured 28.1s per-call cost IS that construction.

    Returns the empty string — never an error — for every case where there is
    nothing to say: no readable page, no ``uid`` on the hit, a page class whose
    mapped surface class is not actually excluded, or no matching record.
    """
    from athenaeum import pii

    if page_path is None:
        return ""

    uid = str(fm.get("uid") or "").strip()
    if not uid:
        # A hit with no uid has nothing to join ON. Not an error, and it must
        # produce no redaction marker either — there is no record to be
        # withholding, so a marker would assert something false.
        return ""

    surface_class = pii.surface_class_for_page_class(str(fm.get("type") or ""), config)
    if not surface_class:
        return ""

    # THE GATE, before any join is attempted: a page class whose mapped surface
    # class is not excluded (every class on a base that maps only
    # `pii: excluded`) resolves to the DEFAULT WIKI ADAPTER. Joining there would
    # scan the wiki root as if it were an excluded surface — reading the corpus
    # back as though it were the excluded store. Return nothing instead; never
    # an error.
    if not is_excluded(surface_class, config):
        return ""

    knowledge_root = wiki_root.parent
    index = indexes.get(surface_class)
    if index is None:
        index = pii.ExcludedRecordIndex(
            pii.excluded_surface_root(surface_class, knowledge_root, config)
        )
        indexes[surface_class] = index

    record_path = index.by_uid(uid)
    if record_path is None:
        return ""

    fields, redactions, _ = pii.assemble_excluded_read(
        page_path,
        fm,
        pii.read_bounce_record(record_path),
        surface_class=surface_class,
        config=config,
        include_excluded=True,
        usage_classes=usage_classes,
    )

    lines: list[str] = []
    for field_name, values in fields.items():
        lines.append(f"**{field_name}:** {', '.join(values)}")
    for marker in redactions:
        # Withheld and absent must never collapse to the same shape: a field
        # the caller did not receive is reported AS withheld, with how many
        # values exist, rather than simply not appearing.
        lines.append(f"**{marker.field}:** [redacted — {marker.value_count} value(s) on file]")
    if not lines:
        return ""
    return "".join(f"{line}\n" for line in lines)


def _recall_via_backend(
    wiki_root: Path,
    query: str,
    top_k: int,
    backend_name: str,
    cache_dir: Path | None,
    extra_roots: list[Path],
    caller_audience: set[str] | None = None,
    config: dict[str, object] | None = None,
    *,
    with_pii: bool = False,
    usage_classes: Collection[str] | None = None,
) -> str:
    """Delegate recall to a registered search backend, then format results.

    **How ``with_pii`` layers with audience scoping (issues athenaeum#312/#538).**
    The three existing layers are untouched and the flag touches only the third:

    - **Layer A (index build)** — untouched. Excluded surfaces are kept out of
      the index two ways, neither of them ``is_embedded``: structurally (the
      excluded root is never in the scanned path set at all) and, as a
      belt-and-suspenders drop, by ``is_pii_flagged`` at build. ``with_pii``
      must NEVER put an excluded value into the FTS5 or vector store —
      excluded fields are not searchable, only resolvable on a hit the corpus
      already produced.
    - **Layer B (in-query predicate)** — untouched. ``with_pii`` is not a
      search-time predicate and never widens the candidate set.
    - **Layer C (render, here)** — where the join happens, strictly AFTER (1)
      the ``readable``/:func:`is_page_authorized` fail-closed drop and (2) the
      athenaeum#532 ``recallable`` drop. A hit dropped by either never triggers an
      excluded-surface lookup, so a restricted caller cannot use the flag to
      probe the existence of a record on a page it may not read.
    """
    from athenaeum.search import DegradedIndexError, get_backend

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        return str(exc)

    effective_cache = resolve_cache_dir(cache_dir)

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
    except DegradedIndexError as exc:
        # athenaeum#489: a degraded/unavailable index must surface as an explicit,
        # actionable error — never as silently-wrong flat-scored hits.
        return (
            f"recall index unavailable: {exc} "
            "Rebuild it with `athenaeum reindex` and retry."
        )

    if not hits:
        return f"No wiki pages matched query: {query!r}"

    tokens = tokenize_keyword_query(query)

    # Issue athenaeum#532: only enforce the ``recallable`` policy when the config
    # actually defines a non-default storage policy — a strict no-op (including
    # for unreadable/stale hits) for any base with no ``storage:`` config.
    enforce_recallable = storage_policy_configured(config)

    # Render each hit, applying Layer C (issue athenaeum#312): re-check the FRESH
    # on-disk frontmatter for a restricted caller so a stale index (a page
    # whose audience changed since the last rebuild) cannot leak a forbidden
    # page's title, tags, snippet, OR body. Rendered blocks are collected
    # first so the "Found N" header counts only the authorized hits.
    blocks: list[str] = []
    # Issue athenaeum#711: parallel accumulator of exactly the hits that make it into
    # `blocks` — i.e. what this call ACTUALLY pushes into the session, post
    # every filter below (Layer C authorization, the `recallable` policy, and
    # unreadable-file drops). This is the single point recall assembles a
    # push payload, so it is the single right place to instrument (see
    # `athenaeum.push_metrics` module docstring). Kept separate from `blocks`
    # itself so instrumentation can never influence what is rendered.
    _pushed_hits: list[tuple[str, dict[str, object], str]] = []
    # Issue athenaeum#885: ONE index per surface class for the WHOLE call, shared
    # across all top_k hits — never one scan per hit. Keyed by surface class
    # because different hits can map to different excluded surfaces. Each index
    # loads lazily on its first lookup, so a class no hit resolves to costs
    # nothing, and a call with `with_pii` unset never touches this at all.
    excluded_indexes: dict[str, ExcludedRecordIndex] = {}
    for filename, name, score in hits:
        page_path, display_prefix = _resolve_hit_path(
            filename,
            wiki_root,
            extra_roots,
        )
        body = ""
        display_name: object = name
        tags: object = "\u2014"
        fm: dict[str, object] = {}
        readable = False
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                display_name = fm.get("name", display_name)
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

        # Issue athenaeum#532 (H4): honor the storage-adapter ``recallable`` corpus
        # policy at the same render layer. A hit whose entity class routes to a
        # surface with ``recallable: false`` is dropped even if it slipped into
        # the index — the ``recallable`` capability the storage contract
        # promises, enforced fail-closed against fresh on-disk frontmatter (a
        # class flipped to non-recallable since the last rebuild cannot leak).
        # NO-OP by default: with no ``storage:`` config ``enforce_recallable``
        # is False and this is skipped entirely. If we couldn't read the file we
        # cannot verify the class, so (fail-closed) withhold the hit.
        if enforce_recallable:
            if not readable:
                continue
            page_type = str(fm.get("type") or "")
            if not is_recallable(page_type, config):
                continue

        # Issue athenaeum#885: the excluded-field join, LAST — after both drops
        # above. Skipped entirely (and costing zero scans) when `with_pii` is
        # unset, which is what keeps the default path byte-identical.
        excluded_block = ""
        if with_pii:
            excluded_block = _excluded_block_for_hit(
                page_path,
                fm,
                wiki_root=wiki_root,
                config=config,
                indexes=excluded_indexes,
                usage_classes=usage_classes,
            )

        if isinstance(tags, list):
            tags = ", ".join(tags)
        snip = _snippet(body, tokens) if body else ""
        # Issue athenaeum#325: compact provenance/context header from the FRESH
        # on-disk frontmatter (same ``fm`` the Layer-C re-read populated).
        # Each field omits at its default, so an uncontested/unscoped page
        # stays nearly as terse as before; a contradiction-flagged page
        # surfaces a Status line pointing at the pending-question queue.
        meta_lines = _recall_metadata_lines(fm)
        meta_block = "".join(f"{line}\n" for line in meta_lines)
        blocks.append(
            f"{display_name} (score: {score:.1f})\n"
            f"**Path:** {display_prefix}\n"
            f"**Tags:** {tags}\n"
            f"{meta_block}{excluded_block}\n"
            f"{snip}\n"
        )
        _pushed_hits.append((filename, fm, snip))

    if not blocks:
        return f"No wiki pages matched query: {query!r}"

    parts: list[str] = [f"Found {len(blocks)} matching pages:\n"]
    for rank, block in enumerate(blocks, 1):
        parts.append(f"### {rank}. {block}")

    # Issue athenaeum#711: record the push AFTER blocks are finalized (never before —
    # instrumentation must observe exactly what was pushed, never influence
    # it) and wrapped so any failure here can never surface as a recall
    # failure. Best-effort no-op when instrumentation is disabled (see
    # `config.resolve_push_metrics_enabled`) or no session id is available.
    try:
        from athenaeum import push_metrics

        # Resolve the session id via the single helper (issue athenaeum#734):
        # Claude Code exports CLAUDE_CODE_SESSION_ID, not the CLAUDE_SESSION_ID
        # this path used to read — so the guard was always false and no push
        # record was ever written.
        session_id = push_metrics.resolve_session_id()
        if session_id:
            record = push_metrics.build_push_record(
                session_id=session_id,
                query=query,
                backend=backend_name,
                hits=_pushed_hits,
            )
            push_metrics.record_push(record, cache_dir=cache_dir, config=config)
    except Exception:  # recall must never fail over telemetry
        log.debug("push-metrics: push-record instrumentation failed", exc_info=True)

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
        sources: Per-claim provenance (issue athenaeum#90, design-lock §4 in
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

            4. Channel-split extras (issue athenaeum#326) — the dict form may
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

            BREAKING (issue athenaeum#96): the previous bare-dict heuristic that
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
                "source on every write (issue athenaeum#90).",
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

    # Intake screening (issue athenaeum#320): classify sensitive content and resolve the
    # read-time `access:` label (athenaeum#312) to stamp BEFORE the single append-only
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
    filepath = write_raw_intake(target_dir, final_content)
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

    ``extras`` (issue athenaeum#326) is the channel-split payload keyed for
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
        # Stamp the screener's read-time access label (issue athenaeum#320). Never
        # downgrade an access the caller already set on the content — take the
        # more restrictive of the two (issue athenaeum#312 rank).
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
    config: dict | None = None,
) -> FastMCP:
    """Create and return a configured FastMCP server instance.

    Args:
        raw_root: Path to the raw intake directory.
        wiki_root: Path to the compiled wiki directory.
        search_backend: Search backend: ``"keyword"``, ``"fts5"``, or ``"vector"``.
        cache_dir: Directory for search index files (fts5/vector backends).
        extra_roots: Additional intake roots that were indexed alongside
            the wiki. Passed through to :func:`recall_search` so raw
            intake hits resolve to their on-disk path.
        caller_audience: Read-scope pin for this server process (issue athenaeum#312,
            extended to every tool in athenaeum#538). ``None`` (the default) is the
            owner: every tool behaves as it always has, preserving single-user
            behavior. A non-None role set is a RESTRICTED caller and governs the
            whole process, not just ``recall``:

            - Reads: ``recall`` AND the page-content-bearing list/read tools
              (``list_pending_questions`` / ``list_pending_merges`` /
              ``list_pending_decisions`` / ``read_person``) apply the same
              fail-closed predicate, so a restricted caller cannot route
              around ``recall`` by asking a different tool for the same
              bytes — ``read_person`` never returns a contact value for a
              page it withholds (issue athenaeum#864).
            - Writes: the three human-decision-queue mutators
              (``resolve_question`` / ``resolve_merge`` / ``review_audit_item``)
              fail closed — adjudicating the operator's queue is owner-only.
              ``remember`` stays open (intake is screened downstream, athenaeum#320).

            Pinned HERE by the operator's ``athenaeum serve`` invocation — it is
            deliberately NOT a per-tool argument, so a restricted agent cannot
            widen its own scope by passing a different audience.
        screening: Resolved intake-screening config (issue athenaeum#320) from
            :func:`athenaeum.config.resolve_screening`. ``None`` (default) =
            no screening — every ``remember`` write is unclassified, preserving
            existing behavior. When set, sensitive intake is auto-labeled with
            a read-time ``access:`` level before the append-only write. Pinned
            HERE (not a ``remember()`` tool argument) so a caller cannot
            disable its own screening.
        config: Resolved ``athenaeum.yaml`` config (issue athenaeum#532), threaded to
            ``recall`` so the storage-adapter ``recallable`` corpus policy is
            honored at query time — a class routed to a ``recallable: false``
            surface is never returned by ``recall``. ``None`` (default) is a
            no-op for the default configuration (every class all-true).

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
            "memory merges (issue athenaeum#169). "
            "Use `list_pending_decisions` for the unified 'human decisions "
            "needed' queue (questions + merges in one call, issue athenaeum#401). "
            "Use `read_person` for a one-call person read by uid — it is the "
            "only sanctioned way to read a person's contact data; do not open "
            "the contact surface directly (issue athenaeum#864)."
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
            config=config,
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
            sources: Per-claim provenance (issue athenaeum#90, design-lock §4 in
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

                BREAKING (issue athenaeum#96): bare dicts without the wrapper keys
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
        return list_unanswered(
            pending_path,
            caller_audience=caller_audience,
            knowledge_root=wiki_root.parent,
        )

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
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return _forbidden_result()
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
        """List unresolved merge proposals (issue athenaeum#169).

        Returns the unresolved blocks from ``wiki/_pending_merges.md`` —
        resolver-proposed memory merges awaiting human approval. Each
        item has ``id``, ``merge_target_name``, ``sources`` (paths to the
        source memories), ``rationale``, ``draft_merged_body``,
        ``confidence``, and ``created_at``.

        Read-path bound (issue athenaeum#431, complementing the athenaeum#400 write-path
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
        return _list_pending_merges(
            merges_path,
            config=config,
            full_body=full_body,
            caller_audience=caller_audience,
            knowledge_root=wiki_root.parent,
        )

    @mcp.tool()
    def list_pending_decisions() -> list[dict]:
        """List ALL pending human decisions — questions AND merges (issue athenaeum#401).

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

        Read-path bound (issue athenaeum#431): a merge item's ``payload["sources"]``
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
        return _list_decisions(
            wiki_root,
            max_sources_per_merge=max_sources_per_merge,
            caller_audience=caller_audience,
        )

    @mcp.tool()
    def read_person(
        uid: str,
        include_contact_data: bool = False,
        usage_classes: list[str] | None = None,
    ) -> str:
        """One-call person read by uid, with explicit contact-data inclusion (issue athenaeum#864).

        The ONLY sanctioned way to read a person's contact data — do not open
        the contact surface directly (``docs/one-way-in-one-way-out.md`` §3).
        Returns the person's wiki page; with ``include_contact_data`` left at
        its default ``False``, each withheld contact field is reported as a
        redaction marker (naming the field and that a value exists, never the
        value) rather than silently omitted, so a person with a withheld
        email and a person with no email at all are distinguishable. With
        ``include_contact_data=True``, the actual values are included, read
        from the surface ``pii.contacts_surface_root`` resolves — this tool
        never constructs that path itself.

        Same fail-closed audience scoping as ``recall`` (issue athenaeum#312/#538):
        a restricted caller never receives page content, or any contact
        value, for a page it is not authorized to read.

        Every returned contact value carries its usage classification (issue
        athenaeum#866) in the result's ``classifications``, co-indexed with
        ``contact``: ``observed`` (seen in prior communication with this
        person), ``provider`` (supplied by a data vendor) or ``unclassified``
        (obtained before the marker existed — provenance unknown). Storing and
        syncing an address to an address book is permitted for every class;
        using one to INITIATE contact is permitted only for ``observed``.
        ``unclassified`` is never treated as usable.

        Args:
            uid: The person's durable uid.
            include_contact_data: Set ``True`` to receive the actual contact
                values instead of redaction markers. Default ``False``.
            usage_classes: Return only contact values of these usage classes,
                e.g. ``["observed"]`` for a caller that must not receive a
                provider-sourced address by accident (issue athenaeum#866).
                Default ``None`` — every value, each carrying its class.

        Returns:
            A JSON string (``pii.PersonRead.to_dict()`` shape) — or a
            fail-closed refusal / not-found message, each JSON-encoded the
            same way.
        """
        return person_read(
            wiki_root.parent,
            uid,
            include_contact_data=include_contact_data,
            usage_classes=usage_classes,
            caller_audience=caller_audience,
            config=config,
        )

    @mcp.tool()
    def list_axiom_audit() -> list[dict]:
        """Axiom assignment audit — every slug's status + promote/demote history (athenaeum#434).

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
    def scan_retraction_cascade() -> dict:
        """Flag completed merges that relied on a now-retracted source (athenaeum#435).

        Walks the observation supersession log (issue athenaeum#427) against the
        merge-provenance ledger (issue athenaeum#425): when a retracted observation is
        listed among a merge's supporting ``source_paths``, a ``retraction``
        review item is added to the human decisions queue (surfaced by
        ``list_pending_decisions``) naming the dependent merge, the retracted
        source, and the retraction reason.

        The merge itself is never touched — there is deliberately **no
        auto-unmerge**; whether a merge still holds without a retracted source
        is a human call. Idempotent: a re-scan emits only newly-flagged
        ``(merge, retracted source)`` pairs. Returns
        ``{"flagged": N, "items": [...]}`` for the records newly emitted by
        this scan.
        """
        from athenaeum.config import load_config
        from athenaeum.pii import contacts_surface_root
        from athenaeum.retraction_cascade import scan_retraction_cascade as _scan

        knowledge_root = wiki_root.parent
        config = load_config(knowledge_root)
        contacts_root = contacts_surface_root(knowledge_root, config)
        newly = _scan(wiki_root, contacts_root)
        return {"flagged": len(newly), "items": newly}

    @mcp.tool()
    def calibration_summary() -> dict:
        """Per-tier tier-audit calibration counts (issue athenaeum#438).

        The calibration loop for the tiered reasoning pass: a random audit
        share of T1 rejects and T2 approvals is surfaced (as ``type:
        "audit"`` items in ``list_pending_decisions``) for a human to confirm
        or overturn. This returns, per tier, the counts of ``sampled`` /
        ``reviewed`` / ``overturned`` — the calibration signal at a glance
        (``{"T1": {...}, "T2": {...}}``). Reviewing an audit item never
        re-executes the merge; an overturn is a calibration signal only.
        """
        # Issue athenaeum#518: gate behind the reasoning-tier opt-in. When off, return
        # an explicit not-enabled state instead of a permanent 0/0/0 all-clear
        # that reads as "the tiers ran and are well calibrated".
        from athenaeum.config import (
            load_config,
            resolve_reasoning_tier_auditing_enabled,
        )

        if not resolve_reasoning_tier_auditing_enabled(load_config(wiki_root.parent)):
            return {
                "enabled": False,
                "error": (
                    "tier auditing not enabled (set "
                    "librarian.reasoning_tier_auditing_enabled: true, or "
                    "ATHENAEUM_REASONING_TIER_AUDITING_ENABLED=1)"
                ),
            }

        from athenaeum.calibration import calibration_summary as _summary

        return _summary(wiki_root)

    @mcp.tool()
    def review_audit_item(id: str, human_verdict: str, note: str = "") -> dict:
        """Record a human's confirm/overturn of a sampled audit item (athenaeum#438).

        Args:
            id: The audit item id (from ``list_pending_decisions``, ``type:
                "audit"``).
            human_verdict: The human's verdict. Equal to the tier's original
                verdict = confirm (the original decision is left untouched);
                different = overturn (recorded as a calibration signal only —
                no merge is executed or unwound).
            note: Optional free-text note on the review.

        Returns the review record (including ``overturned``). Errors if the id
        is unknown or already reviewed.
        """
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return {"ok": False, "error_code": "forbidden", "error": _FORBIDDEN_MSG}

        # Issue athenaeum#518: reviewing tier audits is meaningless when the tiers are
        # disabled — gate behind the same opt-in as the summary surface.
        from athenaeum.config import (
            load_config,
            resolve_reasoning_tier_auditing_enabled,
        )

        if not resolve_reasoning_tier_auditing_enabled(load_config(wiki_root.parent)):
            return {
                "ok": False,
                "enabled": False,
                "error": "tier auditing not enabled",
            }

        from athenaeum.calibration import record_audit_review

        try:
            return record_audit_review(
                wiki_root, audit_id=id, human_verdict=human_verdict, note=note
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def resolve_merge(id: str, decision: str, note: str = "") -> dict:
        """Approve or reject a pending merge proposal (issue athenaeum#169, athenaeum#425).

        Args:
            id: The id returned by ``list_pending_merges``.
            decision: ``"approve"`` dispatches on the proposal's write kind
                (issue athenaeum#421 classification). A ``create-merged`` proposal
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
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return _forbidden_result()
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
        # Issue athenaeum#425: present only on a fold-into-existing approve.
        for key in ("folded_sources", "aliases_added", "links_rewritten"):
            if key in result:
                response[key] = result[key]
        return response

    return mcp
