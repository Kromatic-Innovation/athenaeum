# SPDX-License-Identifier: Apache-2.0
"""MCP memory server — read/write gate for an Athenaeum knowledge base.

Registers 15 tools (issue athenaeum#538 — the count was previously under-reported as 2;
issue athenaeum#864 added the now-removed ``read_person`` (athenaeum#888); issue
athenaeum#886 added ``read_entity``; issue athenaeum#964 added ``entity_schema``,
the ONE new schema-query tool — see that issue's "no proliferating typed
interfaces" ratified position; issue athenaeum#965 added ``enumerate_entities``,
the generalized ENUMERATION primitive — a distinct code path from ``recall``,
not an argument on it, because it takes no query text and never routes through
relevance ranking):

  Reads:  recall, list_pending_questions, list_pending_merges,
          list_pending_decisions, list_axiom_audit, scan_retraction_cascade,
          calibration_summary, read_entity, entity_schema,
          enumerate_entities
  Writes: remember, resolve_question, resolve_merge, review_audit_item

Excluded/withheld fields (a person's contact data, and whatever else the
operator routes off-corpus) are read through ONE path, in two shapes of the
same read: ``recall(with_pii=True)`` when searching, ``read_entity`` when the
caller already holds a uid. (The person-shaped ``read_person`` wrapper over
``read_entity`` was removed in athenaeum#888 once every known consumer had
migrated.)

Audience scoping (issue athenaeum#312, athenaeum#538). ``caller_audience`` is pinned ONCE at
``create_server`` time (never a per-tool argument, so a restricted agent cannot
widen its own scope) and governs the whole process:

  - ``recall`` and every page-content-bearing LIST/READ tool
    (``list_pending_questions`` / ``list_pending_merges`` /
    ``list_pending_decisions`` / ``read_entity``) apply the
    SAME fail-closed read predicate — a restricted caller sees only pending
    items (or an entity page) whose source pages they are authorized to read,
    so no tool returns page content ``recall`` would withhold. ``read_entity``
    additionally never returns an excluded value for a page
    it withholds (issues athenaeum#864, athenaeum#886). ``recall``'s
    ``with_pii`` join runs strictly AFTER that predicate, so it can never be
    used to probe whether a record exists behind a page the caller may not
    read (issue athenaeum#885).
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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.config import (
    resolve_cache_dir,
    resolve_person_registry_root,
    resolve_push_token_budget,
)
from athenaeum.entity_schema import (
    QUERYABLE_FIELDS,
    declared_entity_classes,
    resolve_entity_classes_cached,
)
from athenaeum.enumeration import DEFAULT_LIMIT as _ENUMERATE_DEFAULT_LIMIT
from athenaeum.enumeration import enumerate_entities as _enumerate_entities
from athenaeum.enumeration import predicate_from_dict as _predicate_from_dict
from athenaeum.killswitch import is_disabled
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    SOURCE_TYPES,
    EntityIndex,
    coerce_bucket,
    is_page_authorized,
    parse_bucket,
    parse_frontmatter,
    render_frontmatter,
    resolve_page_type,
    valid_until_expired,
    validity_bound_str,
)
from athenaeum.person_registry import PersonRegistry
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
    history: bool = False,
    type_filter: str | Sequence[str] | None = None,
    unprompted: bool = False,
    session_scope: str | None = None,
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
            :func:`entity_read`'s — the audience check decides whether the hit
            exists at all, and for a hit that survives it the flag yields
            values. Who may SET the flag remains the deferred athenaeum#864
            question; this parameter neither widens nor narrows it.
        usage_classes: Restrict resolved excluded values to these usage classes
            (issue athenaeum#866), threaded to the join exactly as
            :func:`athenaeum.pii.read_entity` accepts it. ``None`` (default)
            returns every value. Only meaningful with *with_pii* — the flag is
            not usage-class-blind, so a caller that must not receive a
            provider-sourced address (``docs/security-posture.md`` §2.3) can
            filter it here the same way ``read_entity``'s callers already can.
        history: Opt into a HISTORY query (issue athenaeum#904) — the caller is
            explicitly asking about the past, not the current state, so an
            expired ``daily``-bucket page must NOT be deprioritized relative
            to a fresher one (AC5). Default ``False`` applies AC4's
            currency-aware reorder to every result; ``True`` returns results
            in the backend's own relevance order, unchanged. Deliberately the
            most conservative opt-in mechanism (an explicit flag, not query-
            text inference) — see ``_recall_via_backend``'s docstring.
        type_filter: Issue athenaeum#964 — narrow the search to one or more entity
            classes (a page's ``type:``). ``None`` (default) searches every
            class, byte-identical to pre-athenaeum#964 behavior. An opaque,
            operator-defined string — NEVER validated against
            ``wiki/_schema/types.md`` here; see the ``entity_schema`` MCP
            tool / :mod:`athenaeum.entity_schema` for the declared/observed
            registry. A value this deployment has never seen returns an
            empty match together with the classes it DOES have, never a
            silent "no results" and never an error.
        unprompted: Issue athenaeum#718 — opt into the UNPROMPTED push path: hits
            are ranked by relevance x tier x coordinate-fit
            (:func:`athenaeum.memory_tiers.push_score`), restricted to the
            ``hot`` retrieval-cost tier, and greedily selected within
            :func:`athenaeum.config.resolve_push_token_budget`'s token
            budget. Default ``False`` is today's behavior, byte-identical:
            every tier is returned in plain relevance order with no budget
            cap — the ``warm`` tier's "explicit recall only" contract.
            Intended for a non-interactive automation deciding what to
            surface into a turn on its own, not for an agent's own
            conversational tool call.
        session_scope: Issue athenaeum#718 — the calling session's scope
            coordinate (the same shape as a page's `claimed_scope`
            frontmatter value, issue athenaeum#714's ``scope`` dimension).
            When supplied, each hit's coordinate fit against this scope is
            computed (:func:`athenaeum.memory_tiers.scope_relation`) and
            reported in the recall hit header's ``**Scope:**`` segment;
            with ``unprompted=True`` it also weights push selection. ``None``
            (default) skips coordinate-fit weighting entirely (neutral
            weight) and the header shows tier only.

    Returns a formatted string of matching wiki pages with relevance scores
    and content snippets.
    """
    top_k = min(top_k, _MAX_TOP_K)

    if not wiki_root.is_dir():
        return f"Wiki directory not found at {wiki_root}."

    # Issue athenaeum#907: a handle-shaped query (an address, or a registry
    # handle framed as a question) answers by exact reverse lookup rather than
    # similarity search. `resolve_handle_query` is a complete no-op \u2014 no
    # lookup of any kind \u2014 when the query is not handle-shaped, so an ordinary
    # query's output below is untouched by this branch existing.
    from athenaeum import identity_resolution, pii

    handle_resolution = identity_resolution.resolve_handle_query(
        wiki_root.parent,
        wiki_root,
        query,
        caller_audience=caller_audience,
        config=config,
        with_pii=with_pii,
        usage_classes=usage_classes,
    )
    if handle_resolution is not None:
        # Issue athenaeum#1002: `contact_values[].source` can carry a raw
        # frontmatter/record value (including a bare YAML date) straight
        # through from `ContactClassification.source` — coerce it the same
        # way every other JSON-emitting read surface does.
        return json.dumps(
            handle_resolution.to_dict(),
            indent=2,
            sort_keys=True,
            default=pii.json_date_default,
        )

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
        history=history,
        type_filter=type_filter,
        unprompted=unprompted,
        session_scope=session_scope,
    )


def entity_read(
    knowledge_root: Path,
    uid: str,
    *,
    page_class: str,
    include_excluded: bool = False,
    usage_classes: Collection[str] | None = None,
    caller_audience: set[str] | None = None,
    config: dict[str, Any] | None = None,
    not_found_label: str = "entity",
) -> str:
    """Read one entity's page by uid, with excluded-field inclusion gated by a flag.

    The MCP-facing wrapper around :func:`athenaeum.pii.read_entity` (issue
    athenaeum#886) and the generalization of :func:`person_read`, which is now a
    thin wrapper over this function with the class fixed to ``person``.

    It applies the SAME fail-closed audience predicate ``recall`` applies
    (:func:`athenaeum.models.is_page_authorized`, re-checked against fresh
    on-disk frontmatter) BEFORE assembling any excluded data — identically on
    this generic path and on the person path, so a restricted caller can never
    obtain via either tool what ``recall`` would withhold for the same page,
    whichever one it calls.

    Args:
        knowledge_root: Root of the knowledge base (parent of ``wiki/``).
        uid: The entity's durable uid.
        page_class: The wiki page ``type:`` (``person``, ``vendor``, …). It is
            mapped to the SURFACE class holding that entity's excluded record
            via :func:`athenaeum.pii.surface_class_for_page_class`, so callers
            of this interface speak in the class they can see on the page and
            never have to know the surface name. Note this argument selects the
            SURFACE, not the page: the page itself is resolved by uid through
            ``EntityIndex``, whatever its ``type:``.
        include_excluded: When ``True``, the actual excluded values are
            included; default ``False`` withholds each behind a redaction
            marker. Who may set it remains the deferred athenaeum#864 question.
        usage_classes: Restrict returned values to these usage classes
            (issue athenaeum#866). ``None`` returns every value.
        caller_audience: Read-scope pin (issues athenaeum#312/#538). ``None`` is the
            owner (no check). A non-None set is checked fail-closed against the
            resolved page's fresh frontmatter.
        config: Resolved ``athenaeum.yaml``, threaded through so the excluded
            surface resolves per the operator's ``storage.mapping``.
        not_found_label: The noun used in the not-found message, so
            :func:`person_read` keeps its exact existing wording
            (``person not found: uid=...``) while the generic tool says
            ``entity``. Presentation only — never a behavioural difference.

    Returns:
        A JSON string: the module's fail-closed refusal shape
        (:func:`_forbidden_result`) for an unauthorized restricted caller, a
        ``{"ok": False, "error": ...}`` message for an unknown uid, or
        :meth:`athenaeum.pii.EntityRead.to_dict`.
    """
    from athenaeum import pii

    wiki_root = knowledge_root / "wiki"
    entity_index = EntityIndex(wiki_root)
    page_path = entity_index.get_by_uid(uid)
    if page_path is None:
        # athenaeum#1394: EntityIndex only scans `wiki_root`, so a `type:
        # person` page relocated by athenaeum#1247 is invisible to it even
        # though it still exists on disk. PersonRegistry is the sanctioned
        # index for wherever `person_registry.root` now points — it defaults
        # to `wiki_root` itself pre-relocation (see
        # `resolve_person_registry_root`), in which case this fallback never
        # fires and behavior on an unmigrated corpus is unchanged. Register
        # a hit directly on the index already built above so the SAME
        # instance, passed to `pii.read_entity` below, resolves it too —
        # rather than building a second index only this function would see.
        registry_root = resolve_person_registry_root(knowledge_root, config)
        entry = PersonRegistry(registry_root).get_by_uid(uid)
        if entry is not None:
            page_path = entry.path
            entity_index._by_uid[uid] = page_path
    if page_path is None:
        return json.dumps(
            {"ok": False, "error": f"{not_found_label} not found: uid={uid!r}"}, indent=2
        )

    # Fail-closed audience check BEFORE any excluded data is assembled (issue
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

    result = pii.read_entity(
        knowledge_root,
        config,
        uid,
        surface_class=pii.surface_class_for_page_class(page_class, config),
        include_excluded=include_excluded,
        usage_classes=usage_classes,
        entity_index=entity_index,
    )
    if result is None:
        return json.dumps(
            {"ok": False, "error": f"{not_found_label} not found: uid={uid!r}"}, indent=2
        )
    # Issue athenaeum#1002: `result.to_dict()["frontmatter"]` is the page's
    # RAW parsed frontmatter, which can carry a `datetime.date`/`datetime`
    # value straight from a bare YAML date — coerce it to ISO-8601 rather
    # than letting `json.dumps` raise on it.
    return json.dumps(result.to_dict(), indent=2, default=pii.json_date_default)


#: Prefix federated off-corpus hits are tagged with before being merged into
#: a corpus hit list (issue athenaeum#984 AC1). A fixed literal, not the
#: off-corpus root's on-disk ``.name`` — the extra-roots convention
#: ``_resolve_hit_path`` otherwise uses matches by directory name, which an
#: operator's off-corpus ``surface_root`` has no reason to share.
_OFF_CORPUS_HIT_PREFIX = "off-corpus/"


def _resolve_hit_path(
    filename: str,
    wiki_root: Path,
    extra_roots: list[Path],
    *,
    off_corpus_root: Path | None = None,
) -> tuple[Path | None, str]:
    """Resolve an indexed filename back to an on-disk path + display label.

    Indexed filenames come in three shapes:

    - Wiki entries: bare name (``lean-startup.md``). Resolved against
      ``wiki_root`` with the ``wiki/`` display prefix.
    - Extra-root entries: ``<root_name>/<relpath>``. The first path
      segment is matched against an extra root's ``.name`` and the
      remainder resolved against that root. Display prefix is
      ``<root_name>/`` so the path a human sees matches the indexed
      filename.
    - Off-corpus entries (issue athenaeum#984): ``off-corpus/<relpath>`` —
      the fixed :data:`_OFF_CORPUS_HIT_PREFIX`, applied only to hits
      federated in from :func:`athenaeum.off_corpus.query_off_corpus`
      (see ``_recall_via_backend``). Resolved against *off_corpus_root*
      when supplied; never confused with an extra root's ``.name`` match.

    Returns ``(path, display_prefix)``. ``path`` is ``None`` when the
    file cannot be located (stale index, renamed directory, or an
    off-corpus hit resolved with no *off_corpus_root* supplied); callers
    should render the hit with an empty body rather than crash.
    """
    if off_corpus_root is not None and filename.startswith(_OFF_CORPUS_HIT_PREFIX):
        rel = filename[len(_OFF_CORPUS_HIT_PREFIX) :]
        return off_corpus_root / rel, filename

    if "/" not in filename:
        # Wiki entry: flat, shallow.
        return wiki_root / filename, f"wiki/{filename}"

    root_name, _, rel = filename.partition("/")
    for root in extra_roots:
        if root.name == root_name:
            return root / rel, filename
    # Unknown root (index built against a different config, or an
    # off-corpus hit with no off_corpus_root supplied). Return the indexed
    # filename verbatim so callers still see what matched rather than a
    # silent empty render.
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


# Same wikilink grammar as ``inference_blocks._WIKILINK_RE`` /
# ``resolutions._WIKILINK_RE`` (Obsidian-style ``[[slug]]`` / ``[[slug|alias]]``).
# Kept as a separate, local pattern object rather than importing a private
# name from another module — matches those modules' own stated rationale for
# not sharing the compiled regex object across modules.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]")


def _extract_outbound_links(body: str) -> list[str]:
    """Return the wikilink targets in a page body, order-preserved, deduped.

    Issue athenaeum#964: the outbound half of "return related records so an agent
    can continue digging" — the cheap, deterministic half the issue scopes in
    (inbound backlinks need a new index and are out of scope). Slug only (the
    ``|alias`` half of ``[[slug|alias]]`` is display text, not a target).
    """
    slugs: list[str] = []
    seen: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        slugs.append(raw)
    return slugs


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


def _is_deprioritized_for_currency(
    fm: dict[str, object], *, as_of: date | None = None
) -> bool:
    """True when *fm* is an EXPIRED daily-bucket page (issue athenaeum#904, AC4).

    Deliberately narrow, per the design brief: only ``bucket: daily`` pages
    are ever deprioritized. ``weekly``/``durable``/unbucketed pages are never
    touched by this predicate — a corpus with no ``bucket:`` anywhere is
    completely unaffected, which is the load-bearing compatibility
    constraint. Reuses the EXISTING athenaeum#308 :func:`valid_until_expired`
    predicate rather than inventing a parallel validity concept — "expired"
    here means exactly what it means everywhere else in this codebase.
    """
    if parse_bucket(fm) != "daily":
        return False
    return valid_until_expired(fm, as_of)


def _reorder_hits_by_currency(
    hits: list[tuple[str, str, float]],
    *,
    wiki_root: Path,
    extra_roots: list[Path],
    off_corpus_root: Path | None = None,
) -> list[tuple[str, str, float]]:
    """Stable-partition *hits* so an expired daily-bucket page sorts after
    every other hit, without changing the relative order within either group
    (issue athenaeum#904, AC4/AC5).

    **Deprioritizes, does not filter.** This only REORDERS the hits the
    backend already selected as the top *n* by relevance — it never widens
    the candidate pool beyond what the caller asked for and never drops a
    hit. An expired daily page that would have appeared in the results still
    appears; it is simply no longer guaranteed to rank above a page that is
    not currency-penalized. Every :class:`SearchBackend` guarantees its
    return is "ordered by relevance, best first" regardless of the backend's
    own score scale/direction (BM25 vs cosine distance vs the keyword
    backend's integer score) — reordering by stable partition, rather than
    by rewriting the numeric ``score``, works identically across all three
    without needing to know or preserve any backend's score semantics.

    Called only when the caller did NOT opt into history mode (see
    ``_recall_via_backend``'s ``history`` parameter) — with no bucket data in
    the corpus at all, every hit is "not deprioritized" and this is a no-op,
    preserving today's order exactly.
    """
    primary: list[tuple[str, str, float]] = []
    deprioritized: list[tuple[str, str, float]] = []
    for hit in hits:
        filename = hit[0]
        page_path, _ = _resolve_hit_path(
            filename, wiki_root, extra_roots, off_corpus_root=off_corpus_root
        )
        fm: dict[str, object] = {}
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
            except (OSError, UnicodeDecodeError):
                fm = {}
        if _is_deprioritized_for_currency(fm):
            deprioritized.append(hit)
        else:
            primary.append(hit)
    return primary + deprioritized


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

    record_meta = pii.read_bounce_record(record_path)
    fields, redactions, classifications = pii.assemble_excluded_read(
        page_path,
        fm,
        record_meta,
        surface_class=surface_class,
        config=config,
        include_excluded=True,
        usage_classes=usage_classes,
    )
    validity = pii.assemble_excluded_validity(record_meta, fields)
    # Reads BOTH surfaces (issue athenaeum#960): `fm` is this hit's own
    # already-resolved page frontmatter, the same value `assemble_excluded_read`
    # above was given — no extra read needed.
    do_not_email = pii.do_not_email_state(record_meta, fm)

    lines: list[str] = []
    for field_name, values in fields.items():
        lines.append(f"**{field_name}:** {', '.join(values)}")
    for marker in redactions:
        # Withheld and absent must never collapse to the same shape: a field
        # the caller did not receive is reported AS withheld, with how many
        # values exist, rather than simply not appearing.
        lines.append(f"**{marker.field}:** [redacted — {marker.value_count} value(s) on file]")
    if do_not_email.marked:
        # Rendered even with no contact field to show: a record may carry ONLY
        # a do-not-email mark, and dropping the whole block then would hide the
        # single fact that most constrains what a caller may do.
        lines.append(f"**{pii.DO_NOT_EMAIL_FIELD}:** marked")
    if not lines:
        return ""

    facts = _excluded_facts_payload(fields, classifications, validity, do_not_email)
    rendered = "".join(f"{line}\n" for line in lines)
    return rendered + _render_facts_block(facts)


def _excluded_facts_payload(
    fields: Mapping[str, list[str]],
    classifications: Mapping[str, list[Any]],
    validity: Mapping[str, list[Any]],
    do_not_email: Any,
) -> dict[str, object]:
    """The STRUCTURED half of the ``with_pii`` block (issue athenaeum#851).

    "Prose is not an interface." The ``**field:** a@b, c@d`` lines above are
    for a human reading a recall result; a consumer implementing its own
    eligibility policy needs the value, its classification and its validity
    state as parseable fields, and must not have to regex them back out of
    rendered markdown.

    Values are co-indexed exactly as :class:`~athenaeum.pii.EntityRead`
    documents — ``values[i]`` / ``classification`` / ``validity`` describe the
    same value — so the JSON carries the same contract the typed read does.
    """
    contact: dict[str, object] = {}
    for field_name, values in fields.items():
        field_classes = list(classifications.get(field_name, ()))
        field_validity = list(validity.get(field_name, ()))
        contact[field_name] = [
            {
                "value": value,
                "classification": (
                    field_classes[position].to_dict()
                    if position < len(field_classes)
                    else None
                ),
                "validity": (
                    field_validity[position].to_dict()
                    if position < len(field_validity)
                    else None
                ),
            }
            for position, value in enumerate(values)
        ]
    return {
        "contact": contact,
        "do_not_email": do_not_email.to_dict(),
    }


def _render_facts_block(facts: Mapping[str, object]) -> str:
    """Fence *facts* as a labelled JSON block appended to the rendered lines.

    ADDITIVE by design: the ``**field:**`` lines are unchanged, so every
    existing reader of this block keeps working, and a machine consumer gets a
    real interface next to them rather than instead of them.

    ``default=pii.json_date_default`` (issue athenaeum#1002), not a bare
    ``str``: a ``classification["source"]`` value can carry a raw frontmatter
    date, and this is the same coercion point ``read_entity`` uses, so
    recall's ``with_pii`` block cannot render a date differently than the
    typed read does.
    """
    from athenaeum import pii

    payload = json.dumps(
        facts, ensure_ascii=False, sort_keys=True, default=pii.json_date_default
    )
    return f"```json athenaeum-excluded-facts\n{payload}\n```\n"


@dataclass
class _RecallRow:
    """One rendered recall hit plus its athenaeum#718 push-selection inputs
    (relevance/tier/scope-relation/token cost) — see `_recall_via_backend`."""

    block: str
    pushed_hit: tuple[str, dict[str, object], str]
    tier: str
    scope_relation: str | None
    relevance: float
    tokens: int


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
    history: bool = False,
    type_filter: str | Sequence[str] | None = None,
    unprompted: bool = False,
    session_scope: str | None = None,
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

    ``history`` (issue athenaeum#904, AC5): the explicit, conservative opt-in
    signal for "this query is asking for history, not the current state" —
    the design brief calls for the MOST conservative detection mechanism, so
    this is a plain boolean rather than inferred from query text (no keyword
    heuristics, no LLM intent classification — the latter would be scope
    creep the issue's Out of scope section explicitly forbids). Default
    ``False`` applies AC4's currency reorder; ``True`` skips it entirely and
    returns hits in the backend's own relevance order, exactly as before this
    issue existed.

    ``unprompted``/``session_scope`` (issue athenaeum#718): see
    :func:`recall_search`'s docstring. ``unprompted=False`` (default) is
    byte-identical to this issue not existing — the tier/coordinate-fit
    re-ranking and token-budget selection below apply ONLY when
    ``unprompted=True``. The recall hit header's tier + matched-scope
    segment (:func:`athenaeum.memory_tiers.tier_scope_header_line`) is
    computed unconditionally, on every call, regardless of ``unprompted``.
    """
    from athenaeum import memory_tiers
    from athenaeum.push_metrics import estimate_tokens
    from athenaeum.search import DegradedIndexError, get_backend, normalize_type_filter

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
            type_filter=type_filter,
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

    # Issue athenaeum#984 (AC1): federate the off-corpus index shard into
    # this SAME recall call. A no-op (``off_corpus_root`` stays ``None``)
    # when ``off_corpus.enabled`` is unset (the default) — see
    # ``athenaeum.off_corpus.query_off_corpus``'s docstring. Queried with
    # the IDENTICAL ``backend_name`` the primary query just used, which is
    # what makes ``merge_ranked_hits``'s score-sort valid (see that
    # function's docstring) — the two hit lists come from the same scorer,
    # just against two different cache dirs/roots, so neither the corpus
    # nor the off-corpus index silently dominates the merge.
    off_corpus_root: Path | None = None
    try:
        from athenaeum import off_corpus

        knowledge_root = wiki_root.parent
        off_corpus_result = off_corpus.query_off_corpus(
            config,
            knowledge_root,
            effective_cache,
            query,
            backend_name=backend_name,
            top_k=top_k,
            caller_audience=caller_audience,
            type_filter=type_filter,
        )
        if off_corpus_result is not None:
            off_corpus_hits, off_corpus_root = off_corpus_result
            tagged_off_corpus_hits = [
                (f"{_OFF_CORPUS_HIT_PREFIX}{filename}", name, score)
                for filename, name, score in off_corpus_hits
            ]
            hits = off_corpus.merge_ranked_hits(hits, tagged_off_corpus_hits, top_k)
    except off_corpus.OffCorpusConfigError as exc:
        # A misconfigured off_corpus (e.g. an adapter root inside the git
        # tree) must never fail an otherwise-good corpus recall — log and
        # fall back to corpus-only hits, matching the "must never raise
        # for an off-corpus problem" contract query_off_corpus documents.
        log.warning("recall: off_corpus query skipped — misconfigured: %s", exc)

    # Issue athenaeum#964: an unrecognized ``type_filter`` value is NOT an error and
    # never reads as a plain "nothing matched" — it names the classes this
    # deployment actually has, computed against the SAME declared/observed
    # registry the schema-query tool reports (never against
    # ``wiki/_schema/types.md`` alone — an observed-only class must count as
    # recognized too).
    unrecognized_note = ""
    normalized_types = normalize_type_filter(type_filter)
    if normalized_types is not None:
        # Issue athenaeum#1194: the MEMOIZED resolver. This ran a full corpus
        # scan on EVERY `recall(type=...)` call — ~28s per call against the
        # real corpus. Under `serve` it is now computed at most once per
        # process; a one-shot CLI is unaffected (it resolves once either way).
        known_names = {
            c.name
            for c in resolve_entity_classes_cached(
                wiki_root, caller_audience=caller_audience
            )
        }
        unrecognized = [t for t in normalized_types if t not in known_names]
        if unrecognized:
            classes_str = ", ".join(sorted(known_names)) if known_names else "(none)"
            unrecognized_note = (
                f"\n\nNote: type filter value(s) {', '.join(unrecognized)} are not "
                f"a recognized entity class on this deployment. Known classes: "
                f"{classes_str}."
            )

    if not hits:
        return f"No wiki pages matched query: {query!r}{unrecognized_note}"

    # Issue athenaeum#904 (AC4/AC5): currency-aware reorder, skipped entirely in
    # history mode. Reorders only — never changes which hits are present or
    # how many, so every filter/count below is unaffected by this call.
    if not history:
        hits = _reorder_hits_by_currency(
            hits,
            wiki_root=wiki_root,
            extra_roots=extra_roots,
            off_corpus_root=off_corpus_root,
        )

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
    # Issue athenaeum#718: rows are collected here first (block text + the tier/
    # relevance/scope inputs `select_for_push` needs) rather than appended
    # straight into `blocks`/`_pushed_hits`, so an `unprompted=True` call can
    # re-rank and budget-select the FINAL set below without re-deriving any
    # of this per-hit work. With `unprompted=False` (default) every row
    # collected here survives unchanged, in the same order — byte-identical
    # to this issue not existing.
    _rows: list[_RecallRow] = []
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
            off_corpus_root=off_corpus_root,
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
        # Issue athenaeum#964: uid + type on EVERY hit — without a uid an agent can
        # only reach `read_entity` by string-parsing the `<uid>-<slug>.md`
        # filename (exactly the storage-layout parsing
        # `docs/one-way-in-one-way-out.md` exists to prevent). Always
        # rendered (never omit-at-default like the athenaeum#325 header above), since
        # "go dig further" is the whole point of the field.
        uid = str(fm.get("uid") or "—")
        page_type = resolve_page_type(fm) or "—"
        # Outbound `[[wikilink]]` targets (issue athenaeum#964) — the cheap,
        # deterministic half of "return related records" the issue scopes in
        # (inbound backlinks need a new index and are explicitly out of
        # scope). Omitted entirely when the page has none, so the hit renders
        # unchanged in that case (no empty "**Links:**" line).
        links_line = ""
        outbound = _extract_outbound_links(body) if body else []
        if outbound:
            links_line = f"**Links:** {', '.join(outbound)}\n"
        # Issue athenaeum#718: tier + matched-scope header segment — computed on
        # EVERY hit, unconditionally (not gated on `unprompted`), so the
        # consuming agent sees why a hit was pushed regardless of which
        # recall mode produced it.
        memory_tier = memory_tiers.resolve_tier(fm, config=config)
        relation = memory_tiers.scope_relation(fm, session_scope)
        tier_scope_line = memory_tiers.tier_scope_header_line(memory_tier, relation)
        tier_scope_block = f"{tier_scope_line}\n" if tier_scope_line else ""
        block = (
            f"{display_name} (score: {score:.1f})\n"
            f"**Path:** {display_prefix}\n"
            f"**Tags:** {tags}\n"
            f"**Uid:** {uid}\n"
            f"**Type:** {page_type}\n"
            f"{meta_block}{tier_scope_block}{links_line}{excluded_block}\n"
            f"{snip}\n"
        )
        _rows.append(
            _RecallRow(
                block=block,
                pushed_hit=(filename, fm, snip),
                tier=memory_tier,
                scope_relation=relation,
                relevance=float(score),
                # Issue athenaeum#718: meter the FULLY RENDERED block --
                # path/tags/uid/type/meta/tier-scope/links/excluded headers
                # plus the snippet -- never just `snip` alone. The token
                # budget must bound what actually gets pushed into the
                # session; metering only the snippet undercounts by the
                # header overhead (which this issue's own tier/scope segment
                # adds to) and lets the budget be consistently overrun.
                tokens=estimate_tokens(block),
            )
        )

    # Issue athenaeum#718: the unprompted push path — restrict to the `hot`
    # tier, re-rank by relevance x tier x coordinate-fit, and greedily
    # select within the configured token budget. `unprompted=False`
    # (default) skips this entirely: `_rows` keeps every hit, in the same
    # relevance order the backend/currency-reorder already produced.
    if unprompted and _rows:
        budget = resolve_push_token_budget(config)
        candidates = [
            memory_tiers.PushCandidate(
                key=i,
                relevance=row.relevance,
                tier=row.tier,
                scope_relation=row.scope_relation,
                tokens=row.tokens,
            )
            for i, row in enumerate(_rows)
        ]
        selected_order = memory_tiers.select_for_push(candidates, token_budget=budget)
        _rows = [_rows[i] for i in selected_order]

    blocks: list[str] = [row.block for row in _rows]
    _pushed_hits: list[tuple[str, dict[str, object], str]] = [row.pushed_hit for row in _rows]
    # Issue athenaeum#1345 AC7: reuse each row's ALREADY-resolved
    # `memory_tiers.resolve_tier` verdict (`row.tier`, computed unconditionally
    # above) for the push-metrics record below — see
    # `push_metrics.build_push_record`'s `memory_tier_by_filename` docstring
    # for why this is resolved HERE rather than inside that function.
    _memory_tier_by_filename: dict[str, str] = {row.pushed_hit[0]: row.tier for row in _rows}

    if not blocks:
        return f"No wiki pages matched query: {query!r}{unrecognized_note}"

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
                memory_tier_by_filename=_memory_tier_by_filename,
            )
            push_metrics.record_push(
                record, cache_dir=cache_dir, config=config, wiki_root=wiki_root
            )
    except Exception:  # recall must never fail over telemetry
        log.debug("push-metrics: push-record instrumentation failed", exc_info=True)

    return "\n".join(parts) + unrecognized_note


def remember_write(
    raw_root: Path,
    content: str,
    source: str = "claude-session",
    *,
    wiki_root: Path | None = None,
    sources: str | dict | None = None,
    screening: dict | None = None,
    bucket: str | None = None,
    valid_until: str | None = None,
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
        bucket: Optional decay classification (issue athenaeum#904) — one of
            ``athenaeum.models.MEMORY_BUCKETS`` (``daily`` / ``weekly`` /
            ``durable``). Rejected at this boundary (an ``Error:`` string,
            not a silent coercion) when set to anything else. ``None``
            (default) writes no ``bucket:`` at all — unset behaves exactly
            as it did before this parameter existed.
        valid_until: Optional SUGGESTED validity end date (``YYYY-MM-DD``,
            issue athenaeum#904). A SUGGESTION, not authoritative: the existing
            athenaeum#308 ``valid_from``/``valid_until`` semantics remain the source
            of truth, so downstream compile/correction only fills this in
            when the target does not already carry an explicit
            ``valid_until`` — it never overrides one. Malformed input is
            fail-open here (dropped, matching every other ``valid_until``
            reader/writer in this codebase — see ``models._coerce_iso_date``),
            not rejected like ``bucket``.

    Returns:
        Confirmation message with the file path, or an error string.
    """
    if len(content.encode("utf-8", errors="replace")) > _MAX_CONTENT_BYTES:
        return f"Error: content exceeds {_MAX_CONTENT_BYTES // (1024 * 1024)} MB limit."

    # Issue athenaeum#904 (AC1): boundary-validate `bucket` before touching the
    # filesystem — same "reject early" discipline the `sources` validation
    # below already follows.
    try:
        coerced_bucket = coerce_bucket(bucket)
    except ValueError as exc:
        return f"Error: invalid `bucket`: {exc}"

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

    # Issue athenaeum#904 (AC1): stamp the optional bucket + suggested valid_until
    # into the SAME `extras` dict the channel-split provenance fields (§326)
    # already ride into `_inject_provenance_frontmatter` on — no new plumbing
    # needed. `coerced_bucket` is "" when unset (no-op). `valid_until` is
    # normalized through the shared fail-open date coercer so a malformed
    # value is silently dropped rather than rejected, matching every other
    # valid_until write path.
    if coerced_bucket:
        extras["bucket"] = coerced_bucket
    if valid_until:
        normalized_valid_until = validity_bound_str(
            {"valid_until": valid_until}, "valid_until"
        )
        if normalized_valid_until:
            extras["valid_until"] = normalized_valid_until

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
              ``list_pending_decisions`` / ``read_entity``) apply the same
              fail-closed predicate, so a restricted caller cannot route
              around ``recall`` by asking a different tool for the same
              bytes — ``read_entity`` never returns an excluded value for a
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

    # Issue athenaeum#964 wired the entity-class registry here, resolved ONCE at
    # construction, to back BOTH the ``recall``/``enumerate_entities`` tool
    # descriptions AND the ``entity_schema`` tool, so the three never drift.
    #
    # Issue athenaeum#1194: that resolution is a full corpus scan, and NOTHING may
    # run a corpus-proportional scan on this path. ``create_server`` is called
    # before ``serve`` answers the MCP ``initialize`` handshake, and a client's
    # ``tools/list`` follows immediately — so a scan here (28s against the real
    # 23.1k-page corpus) blew the client's 30s connect budget and the athenaeum
    # MCP server failed to connect in EVERY session. The split:
    #
    # - DECLARED classes (``wiki/_schema/types.md``) are read here. That is one
    #   small file — O(1) in corpus size — and it is what the tool-schema
    #   descriptions are built from.
    # - OBSERVED classes, live page counts, and the per-class field-key union
    #   all require the scan, so they are resolved LAZILY, on the first tool
    #   call that actually needs them, and memoized for the process.
    #
    # The visible consequence, stated plainly because it walks back part of
    # athenaeum#964: the ``type`` parameter descriptions now enumerate this
    # deployment's DECLARED classes, not declared-plus-observed. An
    # observed-undeclared class (athenaeum#964's own ``auto-memory`` case) is still
    # fully usable as a ``type``/``entity_type`` value, and the ``entity_schema``
    # tool remains authoritative for the complete live list — the descriptions
    # say so. See docs/recall-architecture.md.
    #
    # A ``types.md`` or corpus edit still takes effect on the NEXT server start
    # (unchanged from athenaeum#964; nothing here precludes wiring
    # `notifications/tools/list_changed` later for a live refresh).
    _declared_class_names = sorted(declared_entity_classes(wiki_root))
    _entity_classes_str = (
        ", ".join(_declared_class_names) if _declared_class_names else "(none yet)"
    )

    def _entity_classes():
        """This deployment's resolved classes — the DEFERRED corpus scan.

        Never called during ``create_server``; only from a tool body. Memoized
        per (wiki_root, caller_audience) for the life of the process, so the
        scan is paid at most once however many tools consult it.
        """
        return resolve_entity_classes_cached(wiki_root, caller_audience=caller_audience)

    def _enumerate_known_classes():
        """Issue athenaeum#965's precomputed class set, now computed on first use.

        Still deliberately NOT left to ``enumerate_entities``' own
        ``known_classes=None`` fallback: that would re-scan the corpus on every
        single call. This shares the one process-wide memo instead.
        """
        return frozenset(c.name for c in _entity_classes())

    _enumerate_cache_dir = cache_dir or resolve_cache_dir(None)

    mcp = FastMCP(
        "athenaeum",
        instructions=(
            "Knowledge memory server powered by Athenaeum. "
            "Use `remember` to save information to raw intake for later compilation. "
            "Use `recall` to search the compiled wiki for relevant knowledge — narrow "
            "by entity class with `recall(type=...)` when you know what kind of thing "
            "you're looking for (a person, a company, a principle, ...). Call "
            "`entity_schema` first to discover what kinds of things this deployment "
            "holds and which fields you can query on (issue athenaeum#964). "
            "Need every entity of a type matching criteria — not a ranked search "
            "over a phrase? Use `enumerate_entities(entity_type=..., predicates=...)` "
            "instead of `recall`: it takes no query text and never ranks (issue "
            "athenaeum#965) — the generalized primitive behind `athenaeum people`. "
            "Use `list_pending_questions` / `resolve_question` to triage "
            "detector-flagged contradictions, and "
            "`list_pending_merges` / `resolve_merge` to triage resolver-proposed "
            "memory merges (issue athenaeum#169). "
            "Use `list_pending_decisions` for the unified 'human decisions "
            "needed' queue (questions + merges in one call, issue athenaeum#401). "
            "Excluded/withheld fields — a person's contact data, and whatever "
            "else the operator routes off-corpus — are read through ONE path: "
            "`recall` with `with_pii=True` when you are searching, or "
            "`read_entity` when you already have a uid. Never open an "
            "excluded surface directly (issues athenaeum#864, athenaeum#883, "
            "athenaeum#885, athenaeum#886)."
        ),
    )

    def recall(
        query: str,
        top_k: int = 5,
        with_pii: bool = False,
        history: bool = False,
        type: str | None = None,
    ) -> str:
        return recall_search(
            wiki_root,
            query,
            top_k,
            search_backend=search_backend,
            cache_dir=cache_dir,
            extra_roots=extra_roots,
            caller_audience=caller_audience,
            config=config,
            with_pii=with_pii,
            history=history,
            type_filter=type,
        )

    # Issue athenaeum#964: the docstring — and therefore the ``type`` parameter's
    # description FastMCP puts in the generated tool schema — is built HERE,
    # from ``_entity_classes_str`` above, rather than written as a literal.
    # Assigned to ``__doc__`` (not inline in the ``def``) so it can be
    # computed from this deployment's resolved registry, and the function is
    # registered via an explicit ``mcp.tool()(recall)`` call (not the
    # ``@mcp.tool()`` decorator syntax every other tool below uses) so the
    # registration happens AFTER the dynamic docstring is attached.
    recall.__doc__ = f"""Search the knowledge wiki for pages relevant to a query.

        Dispatches to the configured search backend:

        - ``keyword`` (default fallback): in-memory scoring over frontmatter
          and body; integer-ish relevance scores, higher is better.
        - ``fts5``: SQLite FTS5 over a pre-built index; BM25 scores,
          higher is better.
        - ``vector``: chromadb embeddings over a pre-built index; distance
          scores, lower is better.

        A handle-shaped query (an address, or a registry handle framed as a
        question — "who is this address?", "is this address still current?")
        answers by exact reverse lookup instead (issue athenaeum#907): a JSON
        document with the person's ``uid``, display name, entity class, and
        per-value fact fields (usage/provenance classification, bounce
        history, validity dates) — facts only, never an eligibility or action
        predicate. An ordinary query is unaffected; detection is deliberately
        conservative.

        Args:
            query: Search query string (keywords, names, topics — or natural
                language for semantic recall under the vector backend).
            top_k: Maximum number of results to return (default 5).
            with_pii: Also resolve each matching entity's EXCLUDED fields —
                contact data for a person, and whatever the operator routes
                off-corpus for any other entity class (issue athenaeum#885). This is
                the sanctioned way to read excluded data for a hit you found
                here; do not open an excluded surface directly
                (``docs/one-way-in-one-way-out.md`` section 3). Default ``False``, and
                free when unset: no excluded surface is scanned at all.

                It cannot widen what you can see. Excluded values are never
                indexed and are not searchable — the flag attaches a record to
                a hit the corpus already produced and already authorized, after
                every audience and policy filter. A field you do not receive
                comes back as a redaction marker naming the field and how many
                values exist, never as silence.
            history: Opt into a HISTORY query (issue athenaeum#904) — set this when
                you are explicitly asking about the past ("what did the
                daily status say last week?") rather than the current state.
                By default an expired ``daily``-bucket page ranks below a
                current one; ``history=True`` disables that reorder for this
                call and returns results in plain relevance order.
            type: Narrow the search to one entity class (a page's ``type:``) —
                issue athenaeum#964. Omitting this (the default, ``None``) searches
                EVERY class, exactly as before this parameter existed. This
                deployment's DECLARED entity classes: {_entity_classes_str}.
                That list is not exhaustive — a class observed live in the
                corpus but not yet declared is equally valid here. Call the
                `entity_schema` tool for the complete live list, each class's
                page count, and whether it is declared / observed / both
                (issue athenaeum#1194: only the declared half is cheap enough to
                compute while the server is starting up). A value matching no
                class at all still runs — it returns no matches together with
                this deployment's full class list in the response, never a
                silent "nothing matched" and never an error, so a typo is
                always diagnosable from the response alone.

        Returns:
            Matching wiki pages with relevance scores and content snippets.
        """
    mcp.tool()(recall)

    @mcp.tool()
    def entity_schema() -> dict:
        """Report the entity classes this deployment declares and/or observes.

        Issue athenaeum#964: the ONE schema-query tool this issue adds (the
        operator's ratified direction was "the only other endpoint or MCP
        tooling we should be adding is schema queries" — no per-kind API).
        Call this BEFORE narrowing a `recall` query with `type=...` when you
        don't already know this deployment's entity classes.

        Resolved on FIRST call and memoized for the life of the process
        (issue athenaeum#1194) — never at server construction, where a
        corpus-proportional scan would blow the MCP client's connect budget.
        This tool is AUTHORITATIVE for the live class list: `recall`'s and
        `enumerate_entities`' `type` descriptions enumerate only the DECLARED
        classes, because those are built at construction time from
        `wiki/_schema/types.md` alone. An observed-undeclared class appears
        here and is usable as a `type` value, but is not named there.
        A `wiki/_schema/types.md` edit takes effect on the next server start
        (this issue does not implement hot reload — see
        docs/recall-architecture.md).

        Returns:
            A dict with:

            - `classes`: one entry per entity class — `name`, `count` (live
              pages this caller may read), `declared` (present in
              `wiki/_schema/types.md`), `observed` (at least one live page
              carries this class), `fields` (the union of frontmatter KEYS
              its pages carry — keys only, values are never reported, and
              any key routed to an excluded surface, e.g. inline contact
              fields, is omitted entirely rather than listed).
            - `queryable_fields`: the fields `recall`'s filter arguments
              actually implement today — exactly `["type"]`. Never advertises
              a field no filter implements.
        """
        return {
            "classes": [
                {
                    "name": c.name,
                    "count": c.count,
                    "declared": c.declared,
                    "observed": c.observed,
                    "fields": list(c.fields),
                }
                for c in _entity_classes()
            ],
            "queryable_fields": list(QUERYABLE_FIELDS),
        }

    def enumerate_entities(
        entity_type: str,
        predicates: list[dict] | None = None,
        sort_key: str = "name",
        descending: bool = True,
        limit: int = _ENUMERATE_DEFAULT_LIMIT,
        cursor: str | None = None,
        fields: list[str] | None = None,
        with_pii: bool = False,
    ) -> dict:
        try:
            parsed_predicates = [_predicate_from_dict(p) for p in (predicates or [])]
            result = _enumerate_entities(
                wiki_root,
                _enumerate_cache_dir,
                entity_type=entity_type,
                predicates=parsed_predicates,
                sort_key=sort_key,
                descending=descending,
                limit=limit,
                cursor=cursor,
                fields=fields or [],
                with_pii=with_pii,
                caller_audience=caller_audience,
                extra_roots=extra_roots,
                config=config,
                known_classes=_enumerate_known_classes(),
            )
        except ValueError as exc:
            return {
                "hits": [],
                "next_cursor": None,
                "known_classes": [],
                "error": str(exc),
            }
        return {
            "hits": list(result.hits),
            "next_cursor": result.next_cursor,
            "known_classes": list(result.known_classes),
        }

    # Issue athenaeum#965: same reason `recall`'s docstring is built above rather
    # than written as a literal — this deployment's entity-class list is
    # interpolated from `_entity_classes_str`, computed once at server
    # construction. Assigned to `__doc__` (not an inline docstring literal,
    # which an f-string never becomes — CPython only auto-populates
    # `__doc__` from a plain string constant) and registered via an explicit
    # `mcp.tool()(...)` call so registration happens AFTER the dynamic
    # docstring is attached.
    enumerate_entities.__doc__ = f"""Enumerate every entity of a declared type
        matching field predicates.

        Issue athenaeum#965: the generalized ENUMERATION primitive — a DISTINCT
        code path from `recall`, not an argument on it. `recall` narrows a
        RELEVANCE-RANKED search: it always needs query text to rank against, so
        it structurally cannot answer "give me every entity of type X whose
        field Y matches, ordered by field Z" — there is no X to rank. This
        tool takes no query text at all and never routes through
        BM25/vector ranking; it reads a plain type-indexed candidate set and
        applies your predicates directly. Call `entity_schema` first to
        discover this deployment's entity classes and their fields — it is
        authoritative for the live list. This deployment's DECLARED classes
        today: {_entity_classes_str} (not exhaustive — an observed-undeclared
        class is equally valid; see issue athenaeum#1194).

        `athenaeum people` (deprecated by athenaeum#966) has been REMOVED
        (athenaeum#1079) now that this generalized primitive covers every
        reproducible surface — see docs/recall-architecture.md's
        capability-parity table for the historical per-flag mapping.

        Args:
            entity_type: The declared entity class to enumerate (a page's
                `type:`). Required. An unrecognized value does not error —
                the response's `known_classes` names what this deployment
                DOES have, exactly like `recall`'s `type` filter.
            predicates: Field predicates, AND-combined. Each is
                `{{"fields": "name" | ["name", "fallback_name", ...],
                "kind": "eq" | "ne" | "substring" | "regex", "value": "..."}}`.
                `fields` as a list is an ORDERED FALLBACK set, OR-combined —
                the generalized form of `athenaeum people --company`'s
                `current_company` / `linkedin_company_at_connect` shape.
                `eq`/`substring`/`regex` all compare case-insensitively;
                `ne` is `eq` negated (e.g. `do_not_email != true`).
                Default: no predicates (every page of `entity_type`).
            sort_key: Frontmatter field to sort by. Default `"name"`.
            descending: Sort direction. Default `True`. Ties are always
                broken by `uid` ascending, regardless of direction — the
                documented deterministic tiebreak.
            limit: Max rows to return. `0` = unlimited (matching
                `athenaeum people --limit 0`). Default {_ENUMERATE_DEFAULT_LIMIT}.
            cursor: An opaque continuation token from a prior call's
                `next_cursor`. Must be reused with the IDENTICAL
                `entity_type`/`sort_key`/`descending` it was minted under.
            fields: Additional declared field names to include per hit,
                beyond the always-present `uid`/`type`/`name`. A field
                absent from a page is included as `null`, never silently
                omitted, so every hit has the same shape.
            with_pii: Required to reference `google_contact_*` as a
                predicate field OR a requested output field — the SAME flag
                contract `recall(with_pii=...)` already uses. Default
                `False`. `do_not_email` (and its `_reason`/`_date`
                companions) do NOT require this flag (athenaeum#1122) —
                they are plain frontmatter fields, not PII-gated.

        Returns:
            A dict with `hits` (each carrying `uid`, `type`, `name`, plus
            any requested `fields`), `next_cursor` (a token for the next
            page, or `null` when there is none), `known_classes` (populated
            only when `entity_type` was not recognized), and — only on a
            caller-input error such as a PII-gated field used without
            `with_pii=True`, or a cursor minted under a different query
            shape — an `error` string with `hits`/`next_cursor` empty.
        """
    mcp.tool()(enumerate_entities)

    @mcp.tool()
    def remember(
        content: str,
        source: str = "claude-session",
        sources: str | dict | None = None,
        bucket: str | None = None,
        valid_until: str | None = None,
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
            bucket: Optional decay classification (issue athenaeum#904) — one of
                ``daily`` / ``weekly`` / ``durable``. Use ``daily`` for a
                rapidly-overwritten status note that should decay out of
                recall once superseded rather than compete with durable
                facts forever. Rejected (an error string, not a silent
                coercion) if set to anything else. Unset (default) behaves
                exactly as before this parameter existed.
            valid_until: Optional SUGGESTED expiry date (``YYYY-MM-DD``, issue
                athenaeum#904). A suggestion only — never overrides an explicit
                ``valid_until`` the target already carries.

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
            bucket=bucket,
            valid_until=valid_until,
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
        """Record an answer to a pending question (issue athenaeum#908: deferred apply).

        Also resolves a ``type: "confirmation"`` item (issue athenaeum#1290)
        with NO special-casing: a confirmation is a block in
        ``_pending_questions.md`` like any other, and this tool operates on
        the raw block/id, never on ``list_pending_decisions``'s ``type``
        label. Pass the ``id`` ``list_pending_decisions`` reported for the
        confirmation item; ``answer`` becomes its recorded outcome.

        Validates ``id`` against the CURRENT state of
        ``_pending_questions.md`` (unknown / already-answered id fails
        immediately, nothing is written), then writes the answer as a
        decision-answer file under ``raw/answers/`` — the same conformant
        raw-intake record :mod:`athenaeum.decision_answers` uses for merges
        and audit reviews. The actual checkbox flip, source write-back, and
        archival happen deterministically (no LLM call) on the next
        ``athenaeum ingest-answers`` tick, exactly as the pre-existing
        question-answer flow already deferred archival — this just moves
        the mutation itself to that same tick.

        Args:
            id: The id returned by ``list_pending_questions``.
            answer: The answer body (markdown; may be multi-line).

        Returns:
            A dict with:

            - ``ok`` (bool)
            - ``error_code`` (str | None): one of ``id_not_found``,
              ``already_answered``, ``file_missing`` on failure; ``None``
              on success.
            - ``message`` (str): human-readable status.
            - ``deferred`` (bool): ``True`` on success — the answer is
              recorded but NOT YET applied; state changes on the next
              ``ingest-answers`` tick.
            - ``answer_file`` (str | None): path to the written
              decision-answer file on success; ``None`` on failure.
            - ``decision_id`` (str | None): echoes ``id`` on success.
            - ``resolved_block`` (None): kept for shape back-compat; no
              longer populated since the block isn't rewritten here.

            For backward compatibility the dict also includes legacy
            aliases ``block`` (= ``resolved_block``) and ``error``
            (= ``message`` on failure). New callers should prefer
            ``error_code`` + ``message`` + ``deferred``.
        """
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return _forbidden_result()
        if is_disabled("capture", cache_dir=cache_dir):
            return _kill_switch_result()

        from athenaeum.decision_answers import preflight_question, write_decision_answer

        pending_path = wiki_root / "_pending_questions.md"
        ok, error_code, message = preflight_question(pending_path, id)
        if not ok:
            return {
                "ok": False,
                "error_code": error_code,
                "message": message,
                "deferred": False,
                "answer_file": None,
                "decision_id": None,
                "resolved_block": None,
                # legacy aliases:
                "block": None,
                "error": message,
            }

        answer_path = write_decision_answer(
            raw_root, decision_id=id, decision_type="question", verdict=answer
        )
        message = (
            "answer recorded; applied on the next `athenaeum ingest-answers` tick"
        )
        return {
            "ok": True,
            "error_code": None,
            "message": message,
            "deferred": True,
            "answer_file": str(answer_path),
            "decision_id": id,
            "resolved_block": None,
            # legacy aliases:
            "block": None,
            "error": None,
        }

    @mcp.tool()
    def raise_decision(
        question: str = "",
        context: str = "",
        entity: str = "",
        *,
        kind: str = "question",
        raiser: str = "",
        repo: str = "",
        issue_ref: str = "",
        narrowed_scope: str = "",
        implemented_behavior: str = "",
        alternative: str = "",
    ) -> dict:
        """File a NEW agent-raised item into the pending-decisions queue (issue athenaeum#912).

        Before this tool, ``_pending_questions.md`` had exactly one writer —
        athenaeum's own detectors (``tier4_escalate``) — so an agent that
        discovers something needing a human decision had no way to file it.
        The measured harm: during a 2026-08-06 contact-sync fix, a delegated
        agent flagged an ambiguity ("flag it if you meant the stricter
        reading") only in prose to its orchestrator; the flag was folded into
        an unrelated summary, the human answered the OTHER question in that
        summary, and the flag — with no forcing function and no persistent
        state — silently evaporated when the session ended. This tool gives
        that flag somewhere durable to live: the same file-backed sidecar
        detector items already survive their own session through, so it
        participates in the EXISTING ``list_pending_decisions`` render and
        the EXISTING ``resolve_question`` resolve path with no special-casing
        — never a second, parallel queue (which reproduces the original
        problem one level up).

        Args:
            question: The question a human should answer. Required for
                ``kind="question"`` — rejected if empty or all-whitespace
                (``error_code "invalid_question"``). For ``kind=
                "confirmation"`` it is OPTIONAL (issue athenaeum#1290:
                ``question`` is not one of a confirmation's required
                fields) — an empty value is auto-phrased from
                ``implemented_behavior``/``alternative``/``repo``/
                ``issue_ref`` below.
            context: Standalone context a human needs to answer this
                WITHOUT the originating session — the entire reason this
                tool exists is that a session-scoped flag evaporates once
                the session ends. Required for ``kind="question"`` —
                rejected if empty or all-whitespace (``error_code
                "missing_context"``); there is deliberately no default, so a
                contextless raise is never silently accepted. Same
                ``kind="confirmation"`` auto-phrasing exception as
                ``question``.
            entity: Optional short human-readable label. Cosmetic only —
                provenance is carried by a dedicated marker (see below), not
                by this label, so an unlabeled raise is still fully
                distinguishable from a detector item.
            kind: ``"question"`` (default — everything above this line is
                unchanged from before issue athenaeum#1290) or
                ``"confirmation"``. A confirmation is the OTHER shape this
                tool exists for as of athenaeum#1290: an agent that narrowed
                scope mid-build raising "implemented X without Y, confirm?"
                as a durable, non-blocking flag. It renders as a DIFFERENT
                ``type`` (``"confirmation"``, not ``"question"``) in
                ``list_pending_decisions`` output, with a richer payload —
                see the six ``kind="confirmation"``-only args below — but
                resolves through the exact same ``resolve_question`` tool,
                because storage-wise it is still a block in
                ``_pending_questions.md``. An unrecognized ``kind`` is
                rejected (``error_code "invalid_kind"``).
            raiser: Who/what narrowed scope — an agent name, a lane id, etc.
                Required (and validated non-empty) when ``kind=
                "confirmation"``; ignored otherwise.
            repo: The ``owner/repo`` the narrowing happened in.
                Confirmation-only, required.
            issue_ref: The issue or PR the narrowing relates to.
                Confirmation-only, required.
            narrowed_scope: What the agent DIDN'T cover. Confirmation-only,
                required.
            implemented_behavior: What the agent built instead.
                Confirmation-only, required.
            alternative: The road not taken — what a human might have wanted
                instead. Confirmation-only, required.

        Returns:
            A dict with ``ok`` (bool), ``error_code`` (``"invalid_question"``
            | ``"missing_context"`` | ``"invalid_kind"`` |
            ``"missing_confirmation_field"`` | ``"disabled"`` | ``None``),
            ``message`` (str), and ``decision_id`` — on success, the SAME id
            ``list_pending_decisions`` / ``list_pending_questions`` will
            report for this item, usable immediately with
            ``resolve_question`` without a round-trip list call. ``raw_block``
            carries the rendered block text (``None`` on failure); ``block``
            / ``error`` are legacy-shaped aliases for ``raw_block`` /
            ``message``-on-failure, mirroring every other write tool's
            result shape in this module.

            The item is tagged with a provenance marker distinguishing it
            from a detector-raised item — surfaced as ``payload["raised_by"]
            == "agent"`` on the corresponding ``list_pending_decisions``
            entry (``""`` for every detector-raised item, including every
            item that predates this tool). A ``kind="confirmation"`` raise
            additionally surfaces as ``type: "confirmation"`` (not
            ``"question"``) with ``payload`` carrying ``raiser``, ``repo``,
            ``issue_ref``, ``narrowed_scope``, ``implemented_behavior``,
            ``alternative``, and ``raised_at`` (an ISO-8601 UTC timestamp).

        Deliberately NOT owner-gated: unlike ``resolve_question`` /
        ``resolve_merge`` / ``review_audit_item`` (which ADJUDICATE an
        existing queue item and are owner-only per issue athenaeum#538), this tool
        only ever ADDS a new item — the same category of action as
        ``remember``, which is intentionally left open to a restricted
        ``caller_audience`` because intake is write-only and screened
        downstream (see ``_FORBIDDEN_MSG`` above and
        ``TestWriteGuards.test_remember_stays_open_for_restricted_caller``).
        A delegated (restricted) agent is exactly the caller this tool exists
        for — gating it owner-only would defeat the tool's own purpose.
        """
        if is_disabled("capture", cache_dir=cache_dir):
            msg = _KILL_SWITCH_MSG
            return {
                "ok": False,
                "error_code": "disabled",
                "message": msg,
                "decision_id": None,
                "raw_block": None,
                "block": None,
                "error": msg,
            }

        from athenaeum.answers import raise_pending_question

        pending_path = wiki_root / "_pending_questions.md"
        return raise_pending_question(
            pending_path,
            question,
            context,
            entity=entity,
            kind=kind,
            raiser=raiser,
            repo=repo,
            issue_ref=issue_ref,
            narrowed_scope=narrowed_scope,
            implemented_behavior=implemented_behavior,
            alternative=alternative,
        )

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

        Each item is tagged ``type: "question" | "merge" | "confirmation" |
        ...`` and carries the common fields ``id``, ``created_at``,
        ``summary`` (a one-line, answerable question) and ``confidence`` (a
        float for merges, ``null`` for everything else), plus a
        type-specific ``payload``. For a merge the ``summary`` names each
        source page by its human title with a one-line gist, so the decision
        is answerable without opening the raw wiki files. A
        ``type: "confirmation"`` item (issue athenaeum#1290 — see
        ``raise_decision``'s ``kind="confirmation"``) carries ``payload``
        fields ``raiser``, ``repo``, ``issue_ref``, ``narrowed_scope``,
        ``implemented_behavior``, ``alternative``, ``raised_at``. Resolve a
        question OR a confirmation with ``resolve_question`` (both are
        blocks in the same underlying file); resolve a merge with
        ``resolve_merge`` — dispatch on ``type``.

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
    def read_entity(
        uid: str,
        entity_class: str,
        include_excluded: bool = False,
        usage_classes: list[str] | None = None,
    ) -> str:
        """One-call entity read by uid, for ANY entity class (issue athenaeum#886).

        The generic form of the former ``read_person`` tool (removed in
        athenaeum#888) and the sanctioned way to read an
        entity's EXCLUDED fields when you already have its uid — do not open an
        excluded surface directly (``docs/one-way-in-one-way-out.md`` §3). Use
        ``recall(with_pii=True)`` instead when you are searching rather than
        resolving a uid you already hold; both reach the same read.

        Returns the entity's wiki page; with ``include_excluded`` left at its
        default ``False``, each withheld field is reported as a redaction
        marker (naming the field and that a value exists, never the value)
        rather than silently omitted — so an entity with a withheld field and
        an entity with no such field at all stay distinguishable. With
        ``include_excluded=True``, the actual values are included, read from
        whichever surface the class resolves to; this tool never constructs
        that path itself.

        Same fail-closed audience scoping as ``recall`` (issues
        athenaeum#312/#538): a restricted caller never receives page content, or
        any excluded value, for a page it is not authorized to read.

        Every returned value carries its usage classification (issue
        athenaeum#866) in the result's ``classifications``, co-indexed with the
        values: ``observed`` (seen in prior communication), ``provider``
        (supplied by a data vendor) or ``unclassified`` (obtained before the
        marker existed — provenance unknown). Storing and syncing a contact
        value is permitted for every class; using one to INITIATE contact is
        permitted only for ``observed``. ``unclassified`` is never usable.

        Args:
            uid: The entity's durable uid.
            entity_class: The page's ``type:`` — ``person``, ``vendor``, … It
                selects which excluded SURFACE is read (a ``person`` page's
                record lives on the ``pii`` surface); the page itself is
                resolved by uid whatever its type.
            include_excluded: Set ``True`` to receive the actual values
                instead of redaction markers. Default ``False``.
            usage_classes: Return only values of these usage classes, e.g.
                ``["observed"]`` for a caller that must not receive a
                provider-sourced address by accident (issue athenaeum#866).
                Default ``None`` — every value, each carrying its class.

        Returns:
            A JSON string (``pii.EntityRead.to_dict()`` shape) — or a
            fail-closed refusal / not-found message, each JSON-encoded the
            same way.
        """
        return entity_read(
            wiki_root.parent,
            uid,
            page_class=entity_class,
            include_excluded=include_excluded,
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
        # that reads as "the tiers ran and are well calibrated". Issue
        # athenaeum#1200: T1 and T2 are independently armed, so this checks
        # EITHER via resolve_reasoning_tier_any_screen_enabled, not T1's flag
        # alone — a T2-only config must still see T2's sampled audit items.
        from athenaeum.config import (
            load_config,
            resolve_reasoning_tier_any_screen_enabled,
        )

        if not resolve_reasoning_tier_any_screen_enabled(load_config(wiki_root.parent)):
            return {
                "enabled": False,
                "error": (
                    "tier auditing not enabled (set "
                    "librarian.reasoning_tier_auditing_enabled: true for T1, "
                    "and/or librarian.reasoning_tier_t2_auto_apply_enabled: "
                    "true for T2)"
                ),
            }

        from athenaeum.calibration import calibration_summary as _summary

        return _summary(wiki_root)

    @mcp.tool()
    def review_audit_item(id: str, human_verdict: str, note: str = "") -> dict:
        """Record a human's confirm/overturn of a sampled audit item (athenaeum#438,
        deferred apply per athenaeum#908).

        Validates ``id`` against the CURRENT calibration ledger (unknown /
        already-reviewed id fails immediately, nothing is written), then
        writes the review as a decision-answer file under ``raw/answers/``
        — the same conformant raw-intake record used for question answers
        and merge decisions. The actual ledger append (via
        :func:`athenaeum.calibration.record_audit_review`) happens
        deterministically (no LLM call) on the next ``athenaeum
        ingest-answers`` tick.

        NOTE: ``athenaeum calibration review`` (the CLI twin of this tool)
        still calls ``record_audit_review`` directly and immediately —
        athenaeum#908's AC4 names only the three MCP mutators, so this CLI path
        was deliberately left un-deferred. The end state is identical,
        just immediate rather than deferred.

        Args:
            id: The audit item id (from ``list_pending_decisions``, ``type:
                "audit"``).
            human_verdict: The human's verdict. Equal to the tier's original
                verdict = confirm (the original decision is left untouched);
                different = overturn (recorded as a calibration signal only —
                no merge is executed or unwound).
            note: Optional free-text note on the review.

        Returns:
            A dict with ``ok``, ``error_code`` (``id_not_found`` /
            ``already_resolved`` on failure; ``None`` on success),
            ``deferred`` (``True`` on success), ``answer_file``,
            ``decision_id``, and ``error`` (legacy alias, failure only).
            Unlike before athenaeum#908, a success response no longer includes the
            review record itself (``overturned`` etc.) — that is only known
            once the next ``ingest-answers`` tick applies the answer.
        """
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return {"ok": False, "error_code": "forbidden", "error": _FORBIDDEN_MSG}

        # Issue athenaeum#518: reviewing tier audits is meaningless when the tiers are
        # disabled — gate behind the same opt-in as the summary surface.
        # Issue athenaeum#1200: checks EITHER tier's flag (see calibration_summary
        # above for why T1's flag alone is no longer sufficient).
        from athenaeum.config import (
            load_config,
            resolve_reasoning_tier_any_screen_enabled,
        )

        if not resolve_reasoning_tier_any_screen_enabled(load_config(wiki_root.parent)):
            return {
                "ok": False,
                "enabled": False,
                "error": "tier auditing not enabled",
            }

        from athenaeum.decision_answers import preflight_audit, write_decision_answer

        ok, error_code, message = preflight_audit(wiki_root, id)
        if not ok:
            return {
                "ok": False,
                "error_code": error_code,
                "message": message,
                "deferred": False,
                "answer_file": None,
                "decision_id": None,
                "error": message,
            }

        answer_path = write_decision_answer(
            raw_root,
            decision_id=id,
            decision_type="audit",
            verdict=human_verdict,
            note=note,
        )
        message = (
            "review recorded; applied on the next `athenaeum ingest-answers` tick"
        )
        return {
            "ok": True,
            "error_code": None,
            "message": message,
            "deferred": True,
            "answer_file": str(answer_path),
            "decision_id": id,
            "error": None,
        }

    @mcp.tool()
    def resolve_merge(id: str, decision: str, note: str = "") -> dict:
        """Record approve/reject on a pending merge (issue athenaeum#908: deferred apply).

        Validates ``decision`` and ``id`` against the CURRENT state of
        ``_pending_merges.md`` (invalid decision / unknown / already-
        resolved id fails immediately, nothing is written), then writes the
        decision as a decision-answer file under ``raw/answers/`` — the
        same conformant raw-intake record used for question answers and
        audit reviews. The actual merge apply — the write-kind dispatch,
        checkbox flip, wikilink rewrite, source deletes, vector purge, and
        provenance record described below — happens deterministically (no
        LLM call) on the next ``athenaeum ingest-answers`` tick.

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
            A dict with:

            - ``ok`` (bool)
            - ``error_code`` (str | None): one of ``invalid_decision``,
              ``id_not_found``, ``already_resolved``, ``file_missing`` on
              failure; ``None`` on success.
            - ``deferred`` (bool): ``True`` on success — the decision is
              recorded but NOT YET applied; the merge is written on the
              next ``ingest-answers`` tick, so ``folded_sources`` /
              ``aliases_added`` / ``links_rewritten`` are NOT available
              here (they were previously returned synchronously; this is
              the documented deferral trade-off).
            - ``answer_file`` (str | None): path to the written
              decision-answer file on success; ``None`` on failure.
            - ``decision_id`` (str | None): echoes ``id`` on success.
            - ``resolved_block`` (None): kept for shape back-compat.

            For backward compatibility the dict also includes legacy
            aliases ``block`` (= ``resolved_block``) and ``error``
            (= ``message`` on failure), mirroring ``resolve_question``.
            New callers should prefer ``error_code`` + ``message`` +
            ``deferred``.
        """
        # Issue athenaeum#538: adjudicating the human-decision queue is owner-only.
        if caller_audience is not None:
            return _forbidden_result()
        if is_disabled("capture", cache_dir=cache_dir):
            return _kill_switch_result()

        from athenaeum.decision_answers import preflight_merge, write_decision_answer

        merges_path = wiki_root / "_pending_merges.md"
        ok, error_code, message = preflight_merge(merges_path, id, decision)
        if not ok:
            return {
                "ok": False,
                "error_code": error_code,
                "message": message,
                "deferred": False,
                "answer_file": None,
                "decision_id": None,
                "resolved_block": None,
                # legacy aliases:
                "block": None,
                "error": message,
            }

        answer_path = write_decision_answer(
            raw_root,
            decision_id=id,
            decision_type="merge",
            verdict=decision,
            note=note,
        )
        message = (
            "decision recorded; applied on the next `athenaeum ingest-answers` tick"
        )
        return {
            "ok": True,
            "error_code": None,
            "message": message,
            "deferred": True,
            "answer_file": str(answer_path),
            "decision_id": id,
            "resolved_block": None,
            # legacy aliases:
            "block": None,
            "error": None,
        }

    return mcp
