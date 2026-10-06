# SPDX-License-Identifier: Apache-2.0
"""Pending-merge proposal sidecar (issue athenaeum#169, Lane 3) — L4 domain/pipeline.

Contract: owns the ``wiki/_pending_merges.md`` sidecar file end to end —
its block format, parsing, writing (with idempotent id-based dedup),
archiving, and revalidation/retirement of stale queued proposals. Mirrors
the ``_pending_questions.md`` sidecar but for resolver-proposed memory
merges. When the resolver returns ``action="propose_merge"``, the
proposal is appended to ``wiki/_pending_merges.md`` for human approval —
NOT auto-applied.

Factoring rule: this module owns the SIDECAR FILE FORMAT AND STORAGE only
— it does not decide WHETHER a merge should be proposed (that judgment
lives in ``merge.py`` / ``wiki_dedupe.py`` / the resolver) and it does not
decide whether a QUEUED proposal is still valid under today's gates beyond
what ``revalidate_pending_merges`` can prove from the stored block alone
(see that function's own docstring for the narrow, deliberately-incomplete
gate set it re-checks).

SCC membership (L4 domain/pipeline). This module is imported at top level by
``merge.py`` (``write_pending_merge``), ``wiki_dedupe.py``, and ``librarian.py``
(all normal downward dependencies from their side). The librarian-centered
named-8 coupling was dissolved in issue athenaeum#545; this module's OWN cycle with
``merge`` (it hinged on ``_merge_proposal_suppression_reason``, not one of
athenaeum#545's three hoisted primitives) survived as a PRE-EXISTING residual SCC
``{merge, pending_merges, calibration, reasoning_tiers}`` and was dissolved in
issue athenaeum#640:

- ``revalidate_pending_merges`` formerly took a deferred ``from athenaeum.merge
  import _merge_proposal_suppression_reason`` because ``merge.py`` imports
  ``write_pending_merge`` FROM this module at top level, so a module-level import
  of ``merge`` would have been circular. Issue athenaeum#640 hoisted that guardrail DOWN
  to the :mod:`athenaeum.merge_type_gate` leaf, so this module now imports it at
  top level like any other downward dependency and the cycle is gone.

Block format (mirrors ``_pending_questions.md``):

::

    ## [YYYY-MM-DD] Merge: "<merge-target-name>"
    - [ ] Approve this merge? Sources: <path-a>, <path-b>
    **Rationale**: <one sentence>
    **Sources**:
    - <absolute path to source memory a>
    - <absolute path to source memory b>
    **Confidence**: 0.92
    **Draft**:
    ```markdown
    <draft_merged_body>
    ```

The human approves by:

1. Flipping ``- [ ]`` to ``- [x]`` and calling
   :func:`resolve_merge` ("approve") via the MCP tool, OR
2. Calling :func:`resolve_merge` ("reject") with a note. Rejection
   writes a ``refines:`` declaration into one source file so the
   detector's declared-relationship short-circuit stops re-flagging
   the pair (see :mod:`athenaeum.merge` Lane 1 / athenaeum#167).

On the next ``athenaeum ingest-answers`` run (or a dedicated
``ingest-merges`` invocation), approved/rejected blocks are moved to
``_pending_merges_archive.md``.

Nested fences in a Draft body
------------------------------

A Draft body is written between a ```` ```markdown ```` opener and a
bare ```` ``` ```` closer (three backticks). If the draft content itself
needs a fenced snippet (e.g. documenting a shell command), that inner
fence MUST use a different backtick-run length than three — four
backticks (` ```` `) by convention — so it cannot be mistaken for the
outer fence's closer. See :func:`_scan_fence_state` (athenaeum#292).
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from athenaeum.atomic_io import atomic_write_text

# Issue athenaeum#716 (lane 716-B): reuse the ALREADY-EXISTING coordinate
# widening primitive rather than writing a second one (comparator.py's own
# docstring, athenaeum#715 AC9). `comparator` and `pending_merges` are both
# declared L4 (tests/fixtures/layer_declarations.py) -- a same-layer import
# is not an upward edge (test_layer_boundary.py only forbids importer_layer
# < imported_layer), and comparator.py never imports pending_merges (or
# anything that transitively does), so this introduces no cycle either --
# see tests/test_import_graph_acyclic.py, which this change keeps green.
from athenaeum.comparator import _widen_dimension
from athenaeum.dimensions import (
    DEFAULT_REGISTRY,
    Dimension,
    DimensionKind,
    DimensionRegistry,
    coordinate_value,
    parsed_coordinate,
)
from athenaeum.merge_type_gate import _merge_proposal_suppression_reason
from athenaeum.models import (
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    stamp_tombstone,
    tombstone_target,
)

if TYPE_CHECKING:
    # Annotation-only (issue athenaeum#1627) — the real import stays local to
    # `_audit_pending_merge_sources` to avoid paying for
    # `athenaeum.audit_on_touch` (and transitively `athenaeum.audit`) on a
    # write path that never runs an audit (`audit_counters=None`, every
    # pre-athenaeum#1627 caller).
    from athenaeum.audit_on_touch import AuditOnTouchCounters
from athenaeum.provenance import record_merge_provenance
from athenaeum.sidecar_blocks import (
    MARKDOWN_FENCE_OPEN_RE,
    scan_fence_state,
    split_blocks,
)
from athenaeum.store import now_iso
from athenaeum.verdicts import content_hash

log = logging.getLogger(__name__)

# Same overall grammar as :data:`athenaeum.resolutions._WIKILINK_RE`
# (Obsidian-style ``[[slug]]`` / ``[[slug|alias]]``) but with the optional
# ``|alias`` suffix captured as its own group (group 2, including the
# leading ``|``) so a rewrite can repoint the target (group 1) while
# preserving the rendered alias text verbatim. Kept as a SEPARATE pattern
# object rather than adding a capturing group to the shared one — that
# regex is a public module attribute other code may already rely on
# matching group(1) as "the whole match's only group".
_WIKILINK_REWRITE_RE = re.compile(r"\[\[([^\[\]|\n]+?)(\|[^\[\]\n]*)?\]\]")


# Header grammar — ``## [ISO-DATE] Merge: "{name}"``.
_HEADER_RE = re.compile(
    r"^## \[(?P<date>[^\]]+)\] Merge: \"" r"(?P<target>(?:[^\"\\]|\\.)*)" r"\"$"
)
_CHECKBOX_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s*(?P<question>.*)$")


def _scan_fence_state(line: str, fence_len: int) -> int:
    """Return the updated open-fence backtick-length after ``line``.

    ``fence_len`` is the backtick count of the currently open
    ```markdown fence, or ``0`` when no fence is open. Used identically
    by :func:`_split_blocks` and :func:`_parse_block` so a Draft body's
    fence boundaries are recognized the same way in both places.

    A fence only closes on a bare-backtick line whose length EXACTLY
    matches the opening fence's length (not CommonMark's "at least as
    many" rule). This lets a Draft body nest its own fenced snippet by
    opening it with a *different* backtick-run length than the
    enclosing ```markdown fence (e.g. a four-backtick inner fence
    inside the three-backtick outer fence) without prematurely closing
    the outer fence — see the module docstring's nested-fence
    convention.

    Delegates to the shared :func:`athenaeum.sidecar_blocks.scan_fence_state`
    (athenaeum#527) with the pending-merges ```markdown fence-opener so the fence state
    machine has exactly one implementation across both sidecars.
    """
    return scan_fence_state(line, fence_len, fence_open_re=MARKDOWN_FENCE_OPEN_RE)


@dataclass
class PendingMerge:
    """Parsed view of one block in ``_pending_merges.md``."""

    id: str
    merge_target_name: str
    sources: list[str]
    rationale: str
    draft_merged_body: str
    confidence: float
    created_at: str
    resolved: bool
    raw_block: str
    decision: Literal["approve", "reject", ""] = ""
    note: str = ""
    also_affects: list[str] = field(default_factory=list)
    # Issue athenaeum#421: mechanical slug-collision classification recorded at proposal
    # time. ``create-merged`` (slug free) or ``fold-into-existing`` (slug taken
    # by an existing wiki page). Pre-athenaeum#421 blocks lack the line and default to
    # ``create-merged``.
    write_kind: str = "create-merged"
    # Issue athenaeum#602: True iff this block was approved by the T2 auto-finalize
    # path (:func:`resolve_merge` called with ``auto_applied=True``) rather
    # than by a human. Only ever set on an already-resolved (``approve``)
    # block — see ``_rewrite_block_resolved``'s ``**Auto-applied**:`` line,
    # the human-readable twin of the provenance ledger's ``auto_applied``
    # field (:func:`athenaeum.provenance.build_merge_provenance_record`).
    auto_applied: bool = False
    # Issue athenaeum#1142: which embedder (``athenaeum.clusters.EMBEDDER_*``)
    # produced this cluster's vectors, when the caller supplied one (today,
    # only :mod:`athenaeum.wiki_dedupe`'s write path does). ``""`` for a
    # proposal written without this field — pre-athenaeum#1142 blocks, and any
    # caller (e.g. the raw-intake C3 write path in :mod:`athenaeum.merge`)
    # that does not pass ``embedder=`` to :func:`write_pending_merge` — so a
    # pre-existing block still parses without a KeyError and without
    # fabricating an embedder value nobody recorded.
    embedder: str = ""
    # Issue athenaeum#1170 code review: the human-readable name to show a
    # reviewer, when it differs from ``merge_target_name`` (which, for a
    # name-collision proposal, is deliberately the canonical page's
    # FILENAME STEM — see :mod:`athenaeum.name_collisions` — so the fold
    # target derivation resolves correctly for an entity-template
    # ``<uid>-<slug>.md`` page, not just a bare-slug one). ``""`` (the
    # default, and every pre-athenaeum#1170 block) means "no override" —
    # :func:`athenaeum.decisions.merge_to_rich` falls back to
    # ``merge_target_name`` in that case, so a caller that never passes
    # this renders and reads byte-identically to before this field existed.
    # Never affects the block's own header title or the fold-target slug
    # derivation — those still use ``merge_target_name`` — this is a pure
    # display override.
    display_name: str = ""


WRITE_KINDS = ("create-merged", "fold-into-existing")


def identity_slug(path: Path, meta: object) -> str | None:
    """The slug *path* resolves to BY IDENTITY, or ``None``.

    A wiki page identity-resolves to ``slugify(name)`` when its filename is
    either the bare-slug form (``<slug>.md``) or the corpus's real convention,
    ``<uid>-<slug>.md``, where ``<uid>`` is that SAME page's own ``uid:``
    frontmatter (issue athenaeum#1635) — not just any uid-shaped prefix. A
    filename carrying some other prefix (e.g. the ``auto-`` case reported in
    that issue) matches neither form and is deliberately left unresolved:
    widening this to arbitrary prefixes was explicitly out of scope there and
    remains so here.

    This is the SHARED rule (issue athenaeum#1642). It was introduced for
    ``merges propose-fold``'s proposal-time canonical-target check and now
    also backs :func:`find_identity_pages`, so the propose-time check,
    :func:`classify_write_kind` and :func:`resolve_merge`'s approve-time
    target path cannot drift apart. ``meta`` is typed ``object`` (not
    ``dict``) because it is whatever :func:`athenaeum.models.parse_frontmatter`
    returned, which is not guaranteed to be a mapping for a malformed page.
    """
    p_name = meta.get("name") if isinstance(meta, dict) else None
    if not p_name or not str(p_name).strip():
        return None
    slug = slugify(str(p_name))
    if not slug:
        return None
    if path.name == f"{slug}.md":
        return slug
    p_uid = meta.get("uid") if isinstance(meta, dict) else None
    if p_uid and str(p_uid).strip() and path.name == f"{p_uid}-{slug}.md":
        return slug
    return None


def find_identity_pages(merge_target_name: str, wiki_root: Path) -> list[Path]:
    """Every live page under *wiki_root* that owns ``merge_target_name``'s slug.

    Ordered: the bare-slug page (``<slug>.md``) first when it exists, then any
    ``<uid>-<slug>.md`` page that identity-resolves to the same slug per
    :func:`identity_slug`, sorted by filename. Usually 0 or 1 entries — a
    second entry means the slug is genuinely ambiguous (both a bare-slug and a
    uid-prefixed page exist for one name, or two uid-prefixed pages share a
    name), which ``merges propose-fold`` refuses at proposal time.

    **The bare-slug page is matched on filename alone, deliberately.** That is
    exactly what :func:`classify_write_kind` / :func:`resolve_merge` did before
    issue athenaeum#1642, so every pre-existing corpus shape classifies and
    approves byte-identically; the uid-prefixed form is a pure ADDITION,
    reached only when no bare-slug file owns the slug. Pages whose filename
    starts with ``_`` (``_pending_merges.md`` and friends) are never corpus
    pages and are skipped.

    Cost: one ``exists()`` plus a ``glob("*-<slug>.md")`` whose matches are the
    only files whose frontmatter is read — never a full-corpus scan.
    """
    target_slug = slugify(merge_target_name)
    if not target_slug:
        return []
    found: list[Path] = []
    bare = wiki_root / f"{target_slug}.md"
    if bare.is_file():
        found.append(bare)
    try:
        candidates = sorted(wiki_root.glob(f"*-{target_slug}.md"))
    except OSError:
        candidates = []
    for fpath in candidates:
        if fpath.name.startswith("_") or not fpath.is_file():
            continue
        try:
            f_text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        f_meta, _ = parse_frontmatter(f_text)
        if identity_slug(fpath, f_meta) == target_slug:
            found.append(fpath)
    return found


def resolve_target_page(merge_target_name: str, wiki_root: Path) -> Path | None:
    """The single page a merge into *merge_target_name* folds INTO, or ``None``.

    The one target-resolution entry point shared by :func:`classify_write_kind`
    (proposal time) and :func:`resolve_merge` (approve time), so the two can
    never disagree — issue athenaeum#1642's whole point. ``None`` means no
    page owns the slug, i.e. the merge must CREATE the target.

    When :func:`find_identity_pages` returns more than one candidate the first
    is taken, deterministically (bare-slug form, else lowest filename). That
    tie-break is arbitrary but it is the SAME arbitrary choice on both sides,
    which is what preserves the invariant; ``merges propose-fold`` refuses to
    queue an ambiguous fold in the first place.
    """
    pages = find_identity_pages(merge_target_name, wiki_root)
    return pages[0] if pages else None


def classify_write_kind(merge_target_name: str, wiki_root: Path) -> str:
    """Classify a merge proposal by whether its target already exists.

    Returns ``"fold-into-existing"`` when a wiki page already owns the derived
    target slug, else ``"create-merged"`` (issue athenaeum#421). The existence
    check MUST mirror :func:`resolve_merge`'s approve-time target resolution
    EXACTLY so a ``create-merged`` proposal can never later fail
    ``target_exists`` at approve, and a derived ``fold-into-existing``
    proposal can never later fail ``fold_target_missing``. Issue
    athenaeum#1642 makes that mirroring structural rather than a convention
    two call sites have to keep in step by hand: both sides now call
    :func:`resolve_target_page`, so "does a page own this slug" and "which
    file is it" are answered once, by identity (bare-slug **or**
    ``<uid>-<slug>.md`` keyed on the page's own ``uid:``) rather than by
    filename shape alone. Before that fix this side tested only
    ``wiki_root / f"{slugify(name)}.md"``, so a fold into a real
    ``<uid>-<slug>.md`` corpus page classified as ``create-merged`` and, on
    approve, created a duplicate page instead of folding.

    This is the single source of truth for the classification (issue
    athenaeum#748): :func:`write_pending_merge` derives ``write_kind`` from it so
    a caller cannot smuggle in a value that disagrees with reality, and
    :func:`athenaeum.merge._classify_merge_write_kind` delegates here so the
    proposal-time and write-time classifications can never drift apart. It
    lives in this module (not ``merge``) so ``write_pending_merge`` can reach
    it without reintroducing the ``pending_merges`` -> ``merge`` back-edge that
    issue athenaeum#640 dissolved.
    """
    if resolve_target_page(merge_target_name, wiki_root) is not None:
        return "fold-into-existing"
    return "create-merged"


def _make_id(sources: list[str], target_name: str) -> str:
    """Stable id derived from source paths + merge target name.

    Stability contract: id stable across rationale/draft edits; changes
    when the source set or target name changes.
    """
    key = "\n".join(sorted(sources)) + "\n" + target_name.strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _outer_draft_fence(draft_body: str) -> str:
    """Pick a ```markdown fence run longer than any backtick run in the body.

    The ``**Draft**:`` field wraps ``draft_body`` in a ```` ```markdown ````
    ... ```` ``` ```` fence. The reader closes that fence on the first bare
    backtick line whose length EXACTLY matches the opener (see
    :func:`_scan_fence_state`). A merged draft body synthesized by
    :func:`athenaeum.merge.synthesize_body` copies source-memory bodies
    verbatim, so it may itself contain a bare ```` ``` ```` code fence. If the
    outer fence used the same three backticks, that inner fence would close it
    prematurely — leaking the draft's ``## From `<scope>/<file>` `` subsections
    out as bogus top-level blocks that the reader then rejects as "malformed
    headers" and can never archive (issue athenaeum#394, the athenaeum#299/#303 regression).

    Choosing an outer fence one backtick longer than the longest run inside the
    body makes the nested-fence convention documented in the module docstring
    automatic instead of hand-maintained: an inner fence can never match the
    outer fence's length, so it can never close it.
    """
    longest_run = 0
    for match in re.finditer(r"`+", draft_body):
        longest_run = max(longest_run, len(match.group(0)))
    return "`" * max(3, longest_run + 1)


def _escape_quotes(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape_quotes(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def render_block(
    *,
    merge_target_name: str,
    sources: list[str],
    rationale: str,
    draft_merged_body: str,
    confidence: float,
    created_at: str | None = None,
    write_kind: str = "create-merged",
    embedder: str | None = None,
    display_name: str | None = None,
) -> str:
    """Render one pending-merge block as markdown.

    Issue athenaeum#421: ``write_kind`` records the mechanical slug-collision
    classification decided at proposal time — ``create-merged`` (the target
    slug is free) or ``fold-into-existing`` (a wiki page already owns the
    slug). It is CLASSIFICATION only; the fold WRITE path is athenaeum#425.

    Issue athenaeum#1142: ``embedder`` — when a caller supplies one (the same
    value :mod:`athenaeum.clusters` stamps on a formed ``Cluster`` and athenaeum#1032
    already logs) — is rendered as its own ``**Embedder**:`` line. ``None``
    (the default) omits the line entirely, so a caller that never passes it
    (every pre-athenaeum#1142 call site, and the raw-intake C3 write path in
    :mod:`athenaeum.merge`, which is out of this issue's scope) renders a
    byte-identical block to before this change.

    Issue athenaeum#1170 code review: ``display_name`` — when a caller
    supplies one — is rendered as its own ``**Display name**:`` line, read
    back by :func:`athenaeum.decisions.merge_to_rich` in preference to
    ``merge_target_name`` when phrasing the reviewer-facing question. This
    lets a caller (:mod:`athenaeum.name_collisions`) pass a machine-shaped
    ``merge_target_name`` (a filename stem, for correct fold-target
    derivation) without the decision queue ever showing that stem to a
    human. ``None`` (the default) omits the line entirely, so every
    existing caller renders a byte-identical block to before this field
    existed.
    """
    today = created_at or date.today().isoformat()
    target_escaped = _escape_quotes(merge_target_name)
    sources_line = ", ".join(Path(s).name for s in sources) or "(none)"
    parts: list[str] = [
        f'## [{today}] Merge: "{target_escaped}"',
        f"- [ ] Approve this merge? Sources: {sources_line}",
        "",
        f"**Rationale**: {rationale or '(none provided)'}",
        f"**Write kind**: {write_kind}",
    ]
    if embedder:
        parts.append(f"**Embedder**: {embedder}")
    if display_name:
        parts.append(f"**Display name**: {display_name}")
    parts.append("**Sources**:")
    for src in sources:
        parts.append(f"- {src}")
    parts.append(f"**Confidence**: {confidence:.2f}")
    parts.append("**Draft**:")
    fence = _outer_draft_fence(draft_merged_body)
    parts.append(f"{fence}markdown")
    parts.append(draft_merged_body.rstrip("\n"))
    parts.append(fence)
    return "\n".join(parts)


def _split_blocks(text: str) -> list[str]:
    """Split ``_pending_merges.md`` text into per-merge blocks.

    A block's ``**Draft**:`` field is a fenced ```` ```markdown ... ``` ````
    section whose CONTENTS may legitimately contain bare ``---`` lines
    (YAML frontmatter), ``## `` subheadings, or a nested fenced snippet
    (see the module docstring). While inside that fence, lines are never
    treated as block/paragraph delimiters — they are always appended as
    content. Fence tracking is shared with :func:`_parse_block` via
    :func:`_scan_fence_state` so the two can't diverge (athenaeum#292).

    Only a CANONICAL merge header (``## [DATE] Merge: "name"`` — the
    :data:`_HEADER_RE` shape) starts a new top-level block. A bare ``## ``
    line that is not a canonical header — most importantly the
    ``## From `<scope>/<file>` `` subsections that
    :func:`athenaeum.merge.synthesize_body` writes into a draft body — is
    NOT a block boundary: it is appended to the current block when one is
    open, or dropped as inter-block preamble when none is. This is what
    lets a draft whose fence was broken by an inner code fence (issue athenaeum#394)
    re-absorb its leaked ``## From`` subsections into the parent block
    instead of spraying thousands of "malformed header" warnings, and lets
    orphan ``## From`` fragments left behind by an already-archived merge
    drain out of the sidecar on the next rewrite rather than accreting
    forever.

    Delegates to the shared
    :func:`athenaeum.sidecar_blocks.split_blocks` (athenaeum#527) with the canonical
    merge header and the ```markdown fence-opener, so the pending-merges and
    pending-questions splitters share one implementation and cannot diverge
    again.
    """
    return split_blocks(
        text,
        block_header_re=_HEADER_RE,
        fence_open_re=MARKDOWN_FENCE_OPEN_RE,
        context="pending_merges",
    )


def _parse_block(block_text: str) -> PendingMerge | None:
    lines = block_text.splitlines()
    if not lines:
        return None
    header_match = _HEADER_RE.match(lines[0])
    if not header_match:
        log.warning("Skipping merge block with malformed header: %r", lines[0][:80])
        return None
    target_name = _unescape_quotes(header_match.group("target"))
    created_at = header_match.group("date")

    # First non-blank checkbox line determines resolved state.
    resolved = False
    cb_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "":
            continue
        m = _CHECKBOX_RE.match(lines[idx])
        if m:
            resolved = m.group("state").lower() == "x"
            cb_idx = idx
        break
    if cb_idx is None:
        log.warning("Skipping merge block without checkbox: %r", lines[0][:80])
        return None

    rationale = ""
    confidence = 0.0
    sources: list[str] = []
    draft_lines: list[str] = []
    decision = ""
    note = ""
    write_kind = "create-merged"
    auto_applied = False
    embedder = ""
    display_name = ""

    in_sources = False
    in_draft = False
    fence_len = 0

    for raw_line in lines[cb_idx + 1 :]:
        s = raw_line.strip()
        if in_draft:
            new_fence_len = _scan_fence_state(raw_line, fence_len)
            if fence_len:
                if new_fence_len == 0:
                    fence_len = 0
                    in_draft = False
                    continue
                draft_lines.append(raw_line)
                continue
            if new_fence_len:
                fence_len = new_fence_len
                continue
            # Fence opened with no leading marker — accept any content
            # until the next ``**Key**:`` line or block end.
            if s.startswith("**"):
                in_draft = False
            else:
                draft_lines.append(raw_line)
                continue
        if s.startswith("**Rationale**:"):
            in_sources = False
            rationale = s.removeprefix("**Rationale**:").strip()
            continue
        if s.startswith("**Write kind**:"):
            in_sources = False
            parsed_kind = s.removeprefix("**Write kind**:").strip()
            if parsed_kind:
                write_kind = parsed_kind
            continue
        if s.startswith("**Confidence**:"):
            in_sources = False
            raw = s.removeprefix("**Confidence**:").strip()
            try:
                confidence = float(raw)
            except (TypeError, ValueError):
                confidence = 0.0
            continue
        if s.startswith("**Sources**:"):
            in_sources = True
            continue
        if s.startswith("**Draft**:"):
            in_sources = False
            in_draft = True
            fence_len = 0
            continue
        if s.startswith("**Decision**:"):
            in_sources = False
            decision = s.removeprefix("**Decision**:").strip()
            continue
        if s.startswith("**Note**:"):
            in_sources = False
            note = s.removeprefix("**Note**:").strip()
            continue
        if s.startswith("**Auto-applied**:"):
            in_sources = False
            auto_applied = s.removeprefix("**Auto-applied**:").strip().lower() == "true"
            continue
        if s.startswith("**Embedder**:"):
            in_sources = False
            embedder = s.removeprefix("**Embedder**:").strip()
            continue
        if s.startswith("**Display name**:"):
            in_sources = False
            display_name = s.removeprefix("**Display name**:").strip()
            continue
        if in_sources and s.startswith("- "):
            sources.append(s[2:].strip())
            continue
        if in_sources and not s:
            continue
        if in_sources:
            in_sources = False

    draft_body = "\n".join(draft_lines).strip("\n")

    return PendingMerge(
        id=_make_id(sources, target_name),
        merge_target_name=target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_body,
        confidence=confidence,
        created_at=created_at,
        resolved=resolved,
        raw_block=block_text,
        decision=(
            "approve"
            if decision == "approve"
            else "reject" if decision == "reject" else ""
        ),
        note=note,
        write_kind=write_kind,
        auto_applied=auto_applied,
        embedder=embedder,
        display_name=display_name,
    )


def parse_pending_merges(merges_path: Path) -> list[PendingMerge]:
    """Parse ``_pending_merges.md`` into :class:`PendingMerge` objects."""
    if not merges_path.exists():
        return []
    text = merges_path.read_text(encoding="utf-8")
    return [pm for b in _split_blocks(text) if (pm := _parse_block(b)) is not None]


def write_pending_merge(
    merges_path: Path,
    *,
    merge_target_name: str,
    sources: list[str],
    rationale: str,
    draft_merged_body: str,
    confidence: float,
    created_at: str | None = None,
    write_kind: str | None = None,
    embedder: str | None = None,
    display_name: str | None = None,
    audit_client: Any = None,
    audit_model: str = "",
    audit_counters: "AuditOnTouchCounters | None" = None,
    audit_freshness_hours: float | None = None,
    audit_now: "Callable[[], datetime] | None" = None,
) -> str:
    """Append one merge-proposal block to ``_pending_merges.md``.

    ``audit_client`` / ``audit_model`` / ``audit_counters`` /
    ``audit_freshness_hours`` / ``audit_now`` (issue athenaeum#1627): when
    ``audit_counters`` is given, every page named in *sources* is
    re-audited (:func:`athenaeum.audit_on_touch.audit_on_touch`) BEFORE
    this proposal block is written — see :func:`_audit_pending_merge_sources`
    below. ``audit_counters=None`` (every pre-athenaeum#1627 caller)
    disables the hook entirely — byte-identical behavior to before this
    issue.

    Returns the rendered block text (without surrounding separator).
    Creates the file lazily with a ``# Pending Merges`` header. Idempotent:
    if a block with the same id already exists in the file (resolved or
    not), nothing is appended.

    Issue athenaeum#421: ``write_kind`` carries the slug-collision
    classification (``create-merged`` | ``fold-into-existing``).

    Issue athenaeum#1142: ``embedder`` — when supplied — is rendered as a
    ``**Embedder**:`` line (see :func:`render_block`). ``None`` (the
    default) omits it, so a caller that doesn't pass it (e.g. the
    raw-intake C3 write path in :mod:`athenaeum.merge`) writes a
    byte-identical block to before this parameter existed.

    Issue athenaeum#1170 code review: ``display_name`` — when supplied — is
    rendered as a ``**Display name**:`` line (see :func:`render_block`) and
    read back by :func:`athenaeum.decisions.merge_to_rich` in preference to
    ``merge_target_name`` for the reviewer-facing question. ``None`` (the
    default) omits it, so every existing caller writes a byte-identical
    block to before this parameter existed.

    Issue athenaeum#748: ``write_kind`` is DERIVED here, not trusted from the
    caller. The classification is computed from whether the target slug
    already exists under the wiki root (``merges_path.parent`` — the same
    root :func:`resolve_merge` resolves the approve-time target path against
    by default), so a proposal for a slug that does not exist is always
    ``create-merged`` regardless of what the caller passes, and one whose
    slug exists is always ``fold-into-existing``. The parameter is retained
    only as a validated override: passing a ``write_kind`` that DISAGREES
    with the derived classification **fails closed** with a :class:`ValueError`
    rather than storing a block whose ``fold-into-existing`` value would, at
    approve time, delete every source page (the destructive misclassification
    that motivated this issue). ``None`` (the default) simply uses the derived
    value. Passing an unrecognized ``write_kind`` string also fails closed.
    """
    derived_write_kind = classify_write_kind(merge_target_name, merges_path.parent)
    if write_kind is None:
        write_kind = derived_write_kind
    elif write_kind not in WRITE_KINDS:
        raise ValueError(
            f"write_kind must be one of {WRITE_KINDS!r}, got {write_kind!r}"
        )
    elif write_kind != derived_write_kind:
        raise ValueError(
            "write_kind mismatch (athenaeum#748): caller passed "
            f"{write_kind!r} but target slug "
            f"{slugify(merge_target_name)!r} classifies as "
            f"{derived_write_kind!r} under {merges_path.parent}. "
            "Refusing to store a misclassified proposal — a wrong "
            "'fold-into-existing' deletes the source pages at approve time. "
            "Pass write_kind=None to derive it, or correct merge_target_name."
        )
    if audit_counters is not None:
        _audit_pending_merge_sources(
            sources,
            client=audit_client,
            model=audit_model,
            counters=audit_counters,
            freshness_hours=audit_freshness_hours,
            now=audit_now,
        )
    block = render_block(
        merge_target_name=merge_target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_merged_body,
        confidence=confidence,
        created_at=created_at,
        write_kind=write_kind,
        embedder=embedder,
        display_name=display_name,
    )
    block_id = _make_id(sources, merge_target_name)

    if merges_path.exists():
        text = merges_path.read_text(encoding="utf-8")
        existing_ids = {pm.id for pm in parse_pending_merges(merges_path)}
        if block_id in existing_ids:
            log.info("pending_merges: id %s already present; skipping", block_id)
            return block
        combined = text.rstrip() + "\n\n---\n\n" + block + "\n"
    else:
        merges_path.parent.mkdir(parents=True, exist_ok=True)
        combined = "# Pending Merges\n\n" + block + "\n"
    atomic_write_text(merges_path, combined)
    return block


def _audit_pending_merge_sources(
    sources: list[str],
    *,
    client: Any,
    model: str,
    counters: "AuditOnTouchCounters",
    freshness_hours: float | None,
    now: "Callable[[], datetime] | None",
) -> None:
    """Re-audit each page in *sources* before its merge proposal is written
    (issue athenaeum#1627 plan step 3).

    Reads/writes each page directly using the SAME parse-frontmatter ->
    mutate-dict -> render-frontmatter -> atomic-write idiom this module
    already uses for its own sidecar file (and every other in-place page
    editor in this codebase — see :mod:`athenaeum.audit`'s module
    docstring). :func:`athenaeum.audit.apply_verdict_to_meta` (called
    inside :func:`~athenaeum.audit_on_touch.audit_on_touch`) is the ONE
    stamping implementation; this loop only re-reads/writes the file, it
    does not duplicate that mutation logic.

    Never raises and never blocks the proposal write that follows: a
    source that cannot be read, or carries no ``uid:``, is silently
    skipped (nothing to audit/stamp), matching every other best-effort
    page scan in this codebase; :func:`~athenaeum.audit_on_touch.audit_on_touch`
    itself never raises either.
    """
    from athenaeum.audit_on_touch import audit_on_touch

    for raw_path in sources:
        path = Path(raw_path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        uid = meta.get("uid") if meta else None
        if not isinstance(uid, str) or not uid.strip():
            continue

        call_kwargs: dict[str, Any] = {}
        if freshness_hours is not None:
            call_kwargs["freshness_hours"] = freshness_hours
        verdict = audit_on_touch(
            client,
            uid=uid,
            path=path,
            meta=meta,
            body=body,
            model=model,
            counters=counters,
            now=now,
            **call_kwargs,
        )
        if verdict is not None and verdict.error is None:
            atomic_write_text(path, render_frontmatter(meta) + "\n" + body)


def _preview_draft_body(draft_merged_body: str, preview_chars: int) -> tuple[str, bool]:
    """Bound ``draft_merged_body`` to ``preview_chars``.

    Returns ``(text, truncated)``. When the body already fits, ``text`` is
    returned byte-identical (no truncation marker appended) so a normal-sized
    merge's payload is unchanged from before this cap existed (issue athenaeum#431).
    ``preview_chars <= 0`` disables truncation (the resolver already coerces
    non-positive config values back to the default, so this is a defensive
    fallback, not a normal path).
    """
    if preview_chars <= 0 or len(draft_merged_body) <= preview_chars:
        return draft_merged_body, False
    return draft_merged_body[:preview_chars], True


def list_pending_merges(
    merges_path: Path,
    *,
    config: dict | None = None,
    full_body: bool = False,
    caller_audience: set[str] | None = None,
    knowledge_root: Path | None = None,
) -> list[dict]:
    """Return unresolved merges as MCP-friendly dicts.

    Issue athenaeum#431 (read-path defense-in-depth, complementing the athenaeum#400 write-path
    ``max_merge_sources`` suppression): a single oversized pending merge — the
    withdrawn runaway that prompted this issue had a ~878 KB draft body — blew
    out the payload of every ``list_pending_merges`` call because
    ``draft_merged_body`` was returned in full, unbounded. By default this
    truncates ``draft_merged_body`` to
    :func:`athenaeum.config.resolve_merge_body_preview_chars` (env > yaml
    ``librarian.merge_body_preview_chars`` > 2000) characters and adds
    ``draft_merged_body_truncated: True`` plus the untruncated
    ``draft_merged_body_full_length`` so a caller can tell a preview from the
    real thing and decide whether to re-fetch in full.

    Args:
        merges_path: Path to ``wiki/_pending_merges.md``.
        config: Resolved athenaeum config dict (as from
            :func:`athenaeum.config.load_config`), or ``None`` to use the
            resolver's env/default fallback with no yaml override.
        full_body: When ``True``, skip truncation entirely and return the
            complete ``draft_merged_body`` for every item — the on-demand
            escape hatch for a caller that specifically needs the full draft
            (e.g. immediately before approving a merge).

    A body already at or under the cap is returned byte-identical to the
    pre-athenaeum#431 behavior (no truncation marker fields added beyond the two
    always-present booleans/lengths), so normal-sized merges are unaffected.
    """
    from athenaeum.config import resolve_merge_body_preview_chars
    from athenaeum.models import all_sources_authorized

    preview_chars = resolve_merge_body_preview_chars(config)
    out = []
    for pm in parse_pending_merges(merges_path):
        if pm.resolved:
            continue
        # Issue athenaeum#538: a restricted caller sees a merge only if authorized for
        # EVERY source page — the same fail-closed predicate ``recall`` applies,
        # so ``draft_merged_body`` never leaks content ``recall`` would withhold.
        if not all_sources_authorized(
            pm.sources, caller_audience, base=knowledge_root
        ):
            continue
        full = pm.draft_merged_body
        if full_body:
            body, truncated = full, False
        else:
            body, truncated = _preview_draft_body(full, preview_chars)
        out.append(
            {
                "id": pm.id,
                "merge_target_name": pm.merge_target_name,
                "sources": list(pm.sources),
                "rationale": pm.rationale,
                "draft_merged_body": body,
                "draft_merged_body_truncated": truncated,
                "draft_merged_body_full_length": len(full),
                "confidence": pm.confidence,
                "created_at": pm.created_at,
                "write_kind": pm.write_kind,
                "embedder": pm.embedder,
            }
        )
    return out


def _rewrite_block_resolved(
    block_text: str,
    decision: Literal["approve", "reject"],
    note: str,
    *,
    auto_applied: bool = False,
) -> str:
    """Flip the checkbox and tag the block with decision + note.

    ``auto_applied`` (issue athenaeum#602): when ``True``, an additional
    ``**Auto-applied**: true`` line is written — the human-readable marker
    that this ``approve`` was finalized by the T2 reasoning tier's
    auto-finalize path, not by a human reviewing the block. Never written
    on a ``reject`` (auto-finalize never rejects) or on an ordinary
    human approve (the default, ``False``, adds nothing — a pre-athenaeum#602
    resolved block and an ordinary human approve are byte-identical to
    before this existed).
    """
    lines = block_text.splitlines()
    new_lines: list[str] = []
    flipped = False
    for line in lines:
        if not flipped:
            m = _CHECKBOX_RE.match(line)
            if m:
                new_lines.append(f"- [x] {m.group('question').strip()}")
                flipped = True
                continue
        new_lines.append(line)
    new_lines.append("")
    new_lines.append(f"**Decision**: {decision}")
    if note:
        new_lines.append(f"**Note**: {note}")
    if auto_applied:
        new_lines.append("**Auto-applied**: true")
    return "\n".join(new_lines).rstrip() + "\n"


def _add_merge_rejection_declaration(source_path: Path, other_name: str) -> bool:
    """Append ``other_name`` to ``merge_rejected_with:`` in ``source_path``'s frontmatter.

    Issue athenaeum#715. Used by ``resolve_merge(reject)`` to record an
    HONEST, non-directional suppression marker — a human reviewed this
    pair and decided they are NOT the same claim. This is the direct
    replacement for a former ``_add_refines_declaration`` helper that
    wrote this same suppression fact into ``refines:`` (a fabricated
    directional "A refines B" claim that was never adjudicated);
    ``merge._declared_relationship`` reads this field and returns the
    distinct ``"declared-merge-rejection"`` rationale, so Lane 1's
    declared-pair short-circuit still suppresses future detector firings
    on this pair — with NO config gate — while never again writing
    ``refines:`` to record a rejection. Mechanically identical to the
    prior helper (atomic write, idempotent/order-preserving append, same
    error handling) — only the frontmatter key and the value's meaning
    changed. Returns True when the file was modified.

    Migration note: existing corpus pages may already carry fabricated
    ``refines:`` edges written by rejections recorded before this change.
    This function stops writing NEW ones; it does not rewrite any
    existing store — that is a separate, not-yet-scheduled migration.
    """
    if not source_path.is_file():
        log.warning("pending_merges: source file missing: %s", source_path)
        return False
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    target_slug = slugify(other_name)
    rejected_raw = meta.get("merge_rejected_with")
    if isinstance(rejected_raw, list):
        existing = [str(r) for r in rejected_raw]
    elif isinstance(rejected_raw, str) and rejected_raw.strip():
        existing = [rejected_raw.strip()]
    else:
        existing = []
    if any(slugify(r) == target_slug for r in existing):
        return False
    existing.append(other_name)
    meta["merge_rejected_with"] = existing
    new_text = render_frontmatter(meta) + body
    atomic_write_text(source_path, new_text)
    return True


def _source_slugs(sources: list[str]) -> list[str]:
    """Derive the wiki slug each source path would use, deduped, order-preserved.

    Sources on a ``fold-into-existing`` proposal are wiki-tree pages being
    folded away (see :func:`athenaeum.merge._classify_merge_write_kind` —
    the pre-existing-target check that produced this write_kind implies the
    cluster's members are themselves wiki entries, not raw intake). The
    slug is derived from the filename stem exactly like
    :func:`athenaeum.resolutions._build_sibling_index`'s fallback, so it
    matches how the same file would be looked up as a wikilink target.
    """
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        stem = Path(src).stem
        slug = slugify(stem)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _rewrite_inbound_wikilinks(
    wiki_root: Path,
    old_slugs: list[str],
    canonical_slug: str,
    *,
    skip: Path | None = None,
    touched: list[Path] | None = None,
    link_details: list[dict[str, str]] | None = None,
) -> int:
    """Rewrite every ``[[old-slug]]`` / ``[[old-slug|text]]`` link to canonical.

    Walks every ``*.md`` directly under ``wiki_root`` (sidecars like
    ``_pending_merges.md`` are skipped — filenames starting with ``_`` are
    never link targets) and repoints any wikilink whose slugified target
    matches one of ``old_slugs`` at ``canonical_slug``. The rendered
    ``|alias-text`` portion (if present) is preserved verbatim — only the
    link TARGET changes, not the displayed text. ``skip`` excludes the
    canonical page itself (it may legitimately reference its own former
    slug in body prose describing the merge).

    ``touched``, when supplied, has every actually-modified path appended
    to it (issue athenaeum#947 — ``resolve_merge``'s fold commit needs the
    exact set of sibling pages this call rewrote so it can scope its
    ``git add`` pathspec to them; the return value here is only a count).
    Additive optional parameter — :mod:`athenaeum.storage_migrate`'s
    existing call site, which only wants the count, is unaffected.

    ``link_details``, when supplied, has one ``{"path", "from_slug",
    "to_slug"}`` dict appended per (file, old-slug) pair actually rewritten
    in that file — ``path`` relative to ``wiki_root`` (issue athenaeum#716,
    lane 716-B: the merge-provenance ledger's ``links_rewritten`` needs the
    per-link detail, not just a count, to be "sufficient to reverse the
    operation"). One entry per distinct old slug rewritten in a file, not
    one per occurrence — a file with three ``[[old-a]]`` links and one
    ``[[old-b]]`` link, both folded into ``canonical``, gets exactly two
    entries. Additive optional parameter — the existing count-only callers
    (:mod:`athenaeum.storage_migrate`, and this module's own call site when
    it only needs the int) are unaffected by leaving this ``None``.

    Returns the number of files modified. Best-effort: unreadable files are
    skipped, not fatal.
    """
    if not old_slugs:
        return 0
    old_slug_set = set(old_slugs)
    n = 0
    try:
        skip_resolved = skip.resolve() if skip is not None else None
    except OSError:
        skip_resolved = skip
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        if skip is not None:
            try:
                if path.resolve() == skip_resolved:
                    continue
            except OSError:
                if path == skip:
                    continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        matched_old_slugs: set[str] = set()

        def _replace(m: "re.Match[str]") -> str:
            target = m.group(1).strip()
            ts = slugify(target)
            if ts not in old_slug_set:
                return m.group(0)
            matched_old_slugs.add(ts)
            alias_suffix = m.group(2) or ""
            return f"[[{canonical_slug}{alias_suffix}]]"

        new_text = _WIKILINK_REWRITE_RE.sub(_replace, text)
        if new_text != text:
            atomic_write_text(path, new_text)
            n += 1
            if touched is not None:
                touched.append(path)
            if link_details is not None:
                try:
                    rel_path = str(path.relative_to(wiki_root))
                except ValueError:
                    rel_path = path.name
                for old_slug in sorted(matched_old_slugs):
                    link_details.append(
                        {
                            "path": rel_path,
                            "from_slug": old_slug,
                            "to_slug": canonical_slug,
                        }
                    )
    return n


def _add_aliases_to_frontmatter(meta: dict, new_aliases: list[str]) -> dict:
    """Return ``meta`` with ``new_aliases`` unioned into ``aliases:``, deduped.

    Existing ``aliases:`` entries are preserved in order; new ones are
    appended, skipping any already present (by slug equivalence, so
    ``"Old Topic"`` and ``"old-topic"`` are not both recorded). Non-list
    (or absent) existing ``aliases:`` is treated as empty rather than
    raising — a malformed sidecar field should not block the fold.
    """
    existing_raw = meta.get("aliases")
    existing = [str(a) for a in existing_raw] if isinstance(existing_raw, list) else []
    existing_slugs = {slugify(a) for a in existing}
    merged = list(existing)
    for alias in new_aliases:
        if slugify(alias) not in existing_slugs:
            existing_slugs.add(slugify(alias))
            merged.append(alias)
    out = dict(meta)
    if merged:
        out["aliases"] = merged
    return out


def resolve_alias_slug(wiki_root: Path, slug: str) -> str:
    """Resolve ``slug`` to its canonical slug via wiki ``aliases:`` frontmatter.

    Link-time resolution for issue athenaeum#425: a ``[[old-slug]]`` wikilink in a
    not-yet-processed ``raw/`` memory (or any body prose) should resolve to
    the canonical page once ``old-slug`` has been folded away and recorded
    in the canonical page's ``aliases:`` list. Scans every ``*.md`` directly
    under ``wiki_root`` (sidecars excluded) for an ``aliases:`` entry whose
    slugified form matches ``slug``; returns that page's own slug (its
    filename stem) on a hit, else returns ``slug`` unchanged (not an alias,
    or already canonical). First match wins on a (should-not-happen)
    multi-hit; unreadable/malformed files are skipped.

    Intentional, retained helper (issue athenaeum#539 settling of §4.4). It is the
    READ-side of athenaeum#425 (resolving a ``[[old-slug]]`` link to its canonical page
    via ``aliases:``), the complement to — NOT superseded by — the WRITE-side
    :func:`_apply_fold_into_existing` (which folds a page away and records the
    alias). It has no in-repo caller today because the recall/link-rewrite
    consumer that would resolve stale wikilinks on read is not yet wired; the
    resolver itself is correct and tested, so it is kept rather than deleted.
    An intentional internal helper (not on the stable ``__all__`` surface).
    """
    target = slugify(slug)
    if not target:
        return slug
    try:
        candidates = sorted(wiki_root.glob("*.md"))
    except OSError:
        return slug
    for path in candidates:
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not isinstance(meta, dict):
            continue
        aliases_raw = meta.get("aliases")
        if not isinstance(aliases_raw, list):
            continue
        for alias in aliases_raw:
            if slugify(str(alias)) == target:
                return path.stem
    return slug


def _purge_vector_ids(
    slugs: list[str],
    *,
    cache_dir: Path | None,
    search_backend: str | None,
    embedding_model: str | None,
) -> int:
    """Best-effort vector-store purge for deleted wiki slugs (issue athenaeum#425).

    A no-op (returns 0) when ``cache_dir`` is not supplied, the configured
    backend is not ``"vector"``, or chromadb is unavailable — vector purge
    is opportunistic hygiene, never a hard dependency of ``resolve_merge``.
    Filenames are the vector store's id space (see
    :meth:`athenaeum.search.VectorBackend._add_records`), so a slug's id is
    ``"<slug>.md"``.
    """
    if not slugs or cache_dir is None:
        return 0
    if search_backend is not None and search_backend != "vector":
        return 0
    try:
        from athenaeum.search import VectorBackend
    except ImportError:
        return 0
    ids = [f"{slug}.md" for slug in slugs]
    try:
        return VectorBackend(embedding_model=embedding_model).purge_ids(ids, cache_dir)
    except Exception:  # noqa: BLE001 — purge must never break the merge
        log.debug("pending_merges: vector purge skipped for ids=%s", ids)
        return 0


def _same_file(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` denote the same file (issue athenaeum#748).

    Uses :meth:`Path.samefile` when both paths exist (catches hardlinks and
    distinct spellings of one file), and falls back to comparing
    ``resolve()`` d paths otherwise so the check is meaningful even when one
    side has already been removed. Never raises — a comparison error degrades
    to ``False`` (treat as distinct) so this guard can only ever PREVENT a
    delete, never cause one.
    """
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd=root`` (issue athenaeum#947).

    Matches the existing precedent in :func:`athenaeum.corrections._git` /
    :func:`athenaeum.auto_memory_prune._git`. Uses ``check=False`` — callers
    inspect ``.returncode`` themselves — so a git failure degrades to a
    reported condition (or a silent no-op, per call site) rather than
    raising out of a fold. A fold must never crash on a git hiccup; it may
    only ever fail closed via an explicit ``error_code``.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _find_git_repo(wiki_root: Path) -> Path | None:
    """Resolve the git repository containing *wiki_root* (issue athenaeum#947).

    ``_apply_fold_into_existing`` / ``resolve_merge`` are only ever handed
    ``wiki_root`` (conventionally ``<knowledge_root>/wiki``) — never the
    containing ``knowledge_root`` itself. Other modules that gate destructive
    writes on git (:func:`athenaeum.auto_memory_prune.apply_prune`,
    :func:`athenaeum.corrections.retire_batch`) are handed ``knowledge_root``
    directly and can just check ``(knowledge_root / ".git").exists()``; that
    idiom is not available here without threading a brand-new parameter
    through every ``resolve_merge`` caller (the MCP tool, the CLI, every
    test). Instead, ask git itself: run ``git rev-parse --show-toplevel``
    with ``cwd=wiki_root`` and trust its answer — this finds the repo root
    regardless of how many directories separate ``wiki_root`` from it, and
    works identically whether ``wiki_root`` IS the repo root or a
    subdirectory of it.

    Never raises. Any failure — git not installed, ``wiki_root`` missing,
    not inside a work tree, or anything else — degrades to ``None``, which
    the caller (``resolve_merge``) treats as "no git repo" and refuses the
    fold. This function must fail SAFE (refuse), never fail OPEN (proceed
    without git).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(wiki_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top)


# ---------------------------------------------------------------------------
# Coordinate widening / narrowing-invariant (issue athenaeum#716, lane 716-B)
# ---------------------------------------------------------------------------
#
# "Coordinates widen, never narrow": when a fold consolidates several pages'
# separator-dimension coordinates (valid-time, scope, subject, memory-class —
# whichever a deployment's registry declares with ``separates=True``) onto
# one canonical page, the canonical must end up at least as wide as every
# source it absorbed. :func:`_widen_over_metas` computes that widest
# coordinate by folding :func:`athenaeum.comparator._widen_dimension` (the
# EXISTING primitive, athenaeum#715 AC9 — reused here rather than
# reimplemented) across however many metas are involved;
# :func:`_write_coordinate` is its write-side mirror, round-tripping a
# widened value back into the frontmatter key(s) :func:`athenaeum.dimensions
# .coordinate_value` reads it from; :func:`_coordinate_is_at_least_as_wide`
# is the narrowing-invariant predicate itself, called from
# :func:`_apply_fold_into_existing` as a hard refusal (never a later repair)
# per the issue's "silent scope collapse is a fold bug by definition".


def _write_coordinate(meta: dict[str, Any], dimension: Dimension, value: Any) -> None:
    """Write *value* (the shape :func:`_widen_dimension` returns) back into
    *meta* for *dimension*, in place — the inverse of
    :func:`athenaeum.dimensions.coordinate_value` /
    :func:`athenaeum.dimensions.parsed_coordinate`.

    ``valid-time`` is the one kernel dimension split across TWO frontmatter
    keys (``valid_from``/``valid_until``) with an on-disk INCLUSIVE
    ``valid_until`` — :func:`parsed_coordinate`'s own docstring documents the
    exclusive-until conversion at the read boundary; this is that
    conversion's mirror at the write boundary. Every other separator
    dimension (kernel or operator-declared) round-trips through a single
    bare value key, so a generic ``meta[key] = value`` suffices. ``value is
    None`` removes the key rather than writing a null — absent and
    explicitly-null are not the same frontmatter shape, and "no coordinate"
    should read as absent, matching how every bare-key dimension already
    reads a missing key as ``None`` (:func:`coordinate_value`).
    """
    if dimension.name == "valid-time":
        from_date, until_exclusive = value if value is not None else (None, None)
        if from_date is not None:
            meta["valid_from"] = from_date.isoformat()
        else:
            meta.pop("valid_from", None)
        if until_exclusive is not None:
            meta["valid_until"] = (until_exclusive - timedelta(days=1)).isoformat()
        else:
            meta.pop("valid_until", None)
        return
    if dimension.name == "scope":
        key = "claimed_scope"
    elif dimension.name == "subject":
        key = "subject"
    elif dimension.name == "memory-class":
        key = "memory_class"
    else:
        key = dimension.name
    if value is None:
        meta.pop(key, None)
    else:
        meta[key] = value


def _widen_over_metas(dimension: Dimension, metas: list[dict[str, Any]]) -> Any:
    """Fold :func:`_widen_dimension` across *metas* in order.

    Returns the single widest coordinate covering all of them, in the same
    shape :func:`_widen_dimension` itself returns (an ``(from, until)``
    interval tuple, or a raw string for HIERARCHY/ENUM/IDENTITY). ``metas``
    with fewer than one entry returns ``None``; a single entry returns that
    entry's own coordinate (parsed for INTERVAL, raw otherwise) unchanged.
    """
    if not metas:
        return None
    acc: dict[str, Any] = dict(metas[0])
    value = (
        parsed_coordinate(dimension, acc)
        if dimension.kind == DimensionKind.INTERVAL
        else coordinate_value(dimension, acc)
    )
    for meta in metas[1:]:
        value = _widen_dimension(dimension, acc, meta)
        acc = {}
        _write_coordinate(acc, dimension, value)
    return value


def _coordinate_is_at_least_as_wide(
    dimension: Dimension, wide_meta: Mapping[str, Any], narrow_meta: Mapping[str, Any]
) -> bool:
    """True when *wide_meta*'s coordinate on *dimension* is AT LEAST AS WIDE
    as *narrow_meta*'s — the narrowing-invariant predicate (issue athenaeum#716:
    "the narrowing invariant is the mirror of widening"). ``False`` is the
    ONLY failure signal this predicate ever returns; a caller refuses the
    fold on ``False`` rather than attempting a repair.

    An absent coordinate on EITHER side never fails the check — mirrors
    :func:`_widen_dimension`'s own treatment of a missing side (the other
    side's value is taken as-is, neither widened nor narrowed), and matches
    every kernel separator dimension's ``null_means`` semantics (``scope``/
    ``valid-time`` are ``NullMeans.UNIVERSAL`` — absent means "applies
    everywhere", the WIDEST possible state, never narrower than anything).
    """
    if dimension.kind == DimensionKind.INTERVAL:
        wide = parsed_coordinate(dimension, wide_meta)
        narrow = parsed_coordinate(dimension, narrow_meta)
        if wide is None or narrow is None:
            return True
        wide_from, wide_until = wide
        narrow_from, narrow_until = narrow
        from_ok = wide_from is None or (
            narrow_from is not None and narrow_from >= wide_from
        )
        until_ok = wide_until is None or (
            narrow_until is not None and narrow_until <= wide_until
        )
        return from_ok and until_ok
    raw_wide = coordinate_value(dimension, wide_meta)
    raw_narrow = coordinate_value(dimension, narrow_meta)
    if raw_wide is None or raw_narrow is None:
        return True
    if dimension.kind == DimensionKind.HIERARCHY:
        wide_parts = str(raw_wide).strip().lower().split("/")
        narrow_parts = str(raw_narrow).strip().lower().split("/")
        # "wide" is at least as wide as "narrow" iff wide is a prefix of (or
        # equal to) narrow -- the same ancestor-prefix test
        # _widen_dimension's own HIERARCHY branch relies on.
        return wide_parts == narrow_parts[: len(wide_parts)]
    # ENUM / IDENTITY: no graduated width -- "at least as wide" means equal
    # (a real mismatch here means these claims should never have reached a
    # fold together; refusing is the correct, safe outcome).
    return raw_wide == raw_narrow


def _json_safe_coordinate(value: Any) -> Any:
    """Render a widened coordinate value (interval tuples carry ``date``
    objects) into something :func:`json.dumps` accepts, for the
    ``coordinates_widened`` provenance-ledger field."""
    if isinstance(value, tuple):
        return [v.isoformat() if hasattr(v, "isoformat") else v for v in value]
    return value


def _apply_fold_into_existing(
    pm: PendingMerge,
    *,
    target_path: Path,
    target_slug: str,
    wiki_root: Path,
    repo_root: Path,
    cache_dir: Path | None,
    search_backend: str | None,
    embedding_model: str | None,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Execute the ``fold-into-existing`` write path (issue athenaeum#425; issue
    athenaeum#716 lane 716-B turned step 5 from a delete into a tombstone and
    added the fold-graph/coordinate preflight below).

    The target IS the canonical existing page. Steps, in order:

    -1. Fold-graph + coordinate preflight (issue athenaeum#716) — refuses
        BEFORE any mutation whatsoever, same shape as the pre-existing
        ``fold_target_missing``/``no_git_repo`` gates in :func:`resolve_merge`:
        the target must not itself already be a tombstone (keeps
        ``folded_into`` acyclic — a live node has no outgoing edge, so
        nothing can ever loop back to it — AND keeps exactly one live
        canonical per fold set), no folded source may already be a
        tombstone (a tombstone's ``folded_into`` is set once, never
        overwritten by a later, different fold), and the canonical's
        post-fold coordinate on every separator dimension must be at least
        as wide as every folded source's (the mirror check for the widening
        this function performs in step 1 — "silent scope collapse is a
        fold bug by definition").
    0. Take a provenance-snapshot commit (Commit A) of the target page and
       every folded-away source, BEFORE any write below touches a byte
       (issue athenaeum#947 — originally so the step-5 DELETE stayed
       recoverable via plain ``git revert``/``git show`` per ``README.md``'s
       recovery guarantee; athenaeum#716 turned step 5 into a tombstone, which
       does not need git to be recoverable at all — the page is never
       removed — but Commit A is KEPT regardless, because it still protects
       the body overwrite (step 1) and the inbound-link rewrite (step 4),
       exactly as it always has). Callers of this function (only
       :func:`resolve_merge`, for ``write_kind == "fold-into-existing"``)
       have already verified ``repo_root`` is a real git repo before
       calling — see the ``no_git_repo`` gate there — so this step is never
       skipped for that write kind.
    1. Write ``draft_merged_body`` (coordinate-widened per the preflight
       above — see :data:`coordinates_widened` in the return value) to
       ``target_path`` (the merged content — same convention as the
       ``create-merged`` path's body write).
    2. Derive the folded-away source slugs (the OTHER sources — a source
       whose own slug already equals the target is the canonical page
       itself reappearing in its own cluster and is not folded away).
    3. Union those slugs into the canonical page's ``aliases:``
       frontmatter, deduped.
    4. Rewrite every inbound ``[[old-slug]]`` wikilink under ``wiki_root``
       (excluding the canonical page itself) to ``target_slug``.
    5. TOMBSTONE the old source wiki files (issue athenaeum#716 — "a merge may
       destroy renderings, never observations"): stamp each with
       :func:`athenaeum.models.stamp_tombstone` (``status: folded``,
       ``folded_into: <target_slug>``, ``embedded: false``) IN PLACE, via
       ``git add`` (never ``git rm``, never ``Path.unlink()`` — the file
       stays on disk, body untouched, every other frontmatter key —
       including the source's own scope coordinate, "the narrow
       restatement tombstones with its scope noted" — preserved verbatim,
       because :func:`stamp_tombstone` COPIES the meta it is given rather
       than replacing it).
    6. Best-effort purge their vectors from the search index — unchanged;
       this is what keeps a tombstone non-polluting on day one rather than
       waiting for the next reindex to notice ``embedded: false``.
    7. Commit the fold itself as its own commit (Commit B), scoped to
       exactly the paths this fold touched.

    Returns ``{"ok": True, "folded_sources", "aliases_added",
    "links_rewritten", "link_details", "canonical_content_hash",
    "coordinates_widened"}`` on success, or ``{"ok": False, "error_code",
    "message"}`` when the step -1 preflight refuses. ``target_exists`` is
    unreachable from here by construction — the caller only takes this path
    for ``write_kind == "fold-into-existing"``, and athenaeum#421's
    proposal-time classification only assigns that write_kind when the slug
    already exists; this function does not re-check.

    Concurrency (issue athenaeum#947 AC3, extended by athenaeum#1170): this function
    performs real file mutation and git commits. It is reached via TWO
    callers of :func:`resolve_merge`, both already covered by the same
    single-machine run lock:

    1. The deferred apply path (:func:`athenaeum.decision_answers.apply_decision_answers`
       -> this module's :func:`resolve_merge`), which
       :func:`athenaeum._cmd_pending.cmd_ingest_answers` runs after acquiring
       the CLI run lock (issue athenaeum#309, ``_cli_shared._acquire_or_exit``).
    2. :func:`athenaeum.name_collisions.resolve_name_collisions`'s
       unambiguous-collision auto-merge branch (issue athenaeum#1170), called from
       :func:`athenaeum.librarian._run_name_collision_phase`, itself only
       reached from a non-``--dry-run`` :func:`athenaeum.librarian.run` —
       and :func:`athenaeum._cmd_run.cmd_run` only ever calls ``run()`` for a
       real (non-dry-run) invocation AFTER acquiring that identical run
       lock. A ``--dry-run`` invocation never reaches this function at all
       (:func:`athenaeum.name_collisions.resolve_name_collisions` short-
       circuits before any write when ``dry_run`` is true), so the "runs
       under the lock" property holds for every path that can actually call
       this function.

    The MCP ``resolve_merge`` tool (``mcp_server.py``) never calls this
    function directly — issue athenaeum#908 changed it to only validate and
    write a decision-answer file under ``raw/answers/``. So a concurrent
    ``athenaeum run`` is already excluded by the same run lock that guards
    every other mutating CLI command; no additional lock is needed here.
    """
    # Read the PRE-EXISTING target's frontmatter first — its ``aliases:``
    # (accumulated by any prior fold) must survive the draft-body overwrite
    # in step 1 below, so this MUST happen before that write.
    try:
        prior_target_text = target_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        prior_target_text = ""
    prior_meta, _ = parse_frontmatter(prior_target_text)
    if not isinstance(prior_meta, dict):
        prior_meta = {}
    prior_aliases = prior_meta.get("aliases")
    prior_aliases_list = prior_aliases if isinstance(prior_aliases, list) else []
    existing_alias_slugs = {slugify(str(a)) for a in prior_aliases_list}

    # Step -1a (issue athenaeum#716): the target must not itself already be a
    # tombstone. This single check is BOTH fold-graph invariants at once —
    # acyclic (a live node has no outgoing `folded_into` edge, so nothing
    # can ever loop back to a live target; a cycle is only constructible by
    # folding INTO a page that is itself already mid-chain) and exactly-one-
    # live-canonical (a tombstone is, by definition, not the live canonical
    # of anything — designating it as a NEW fold target would create a
    # second claim of canonicity for whatever it already points to).
    if is_tombstone(prior_meta):
        return {
            "ok": False,
            "error_code": "fold_target_is_tombstone",
            "message": (
                f"{target_path} is already a tombstone (folded_into="
                f"{tombstone_target(prior_meta)!r}); refusing to fold more "
                "sources into a non-live canonical. Re-target the proposal "
                "at the live page this one was itself folded into."
            ),
        }

    # Step 2 (computed early, before any write) — folded-away source slugs,
    # excluding the canonical page reappearing among its own sources. Moved
    # ahead of step 1 (the numbering above is the SEMANTIC step order, not
    # this function's statement order) because Commit A below needs the
    # full set of about-to-change paths before any of them are touched.
    # Issue athenaeum#1642: the canonical page is excluded from the folded
    # set by PATH IDENTITY as well as by slug. The slug test alone assumes
    # the target lives at ``<target_slug>.md``; for the real corpus shape
    # ``<uid>-<slug>.md`` the stem slugifies to ``<uid>-<slug>``, so the
    # canonical page fell THROUGH the filter — into the delete list (caught
    # only by step 5's ``_same_file`` defense-in-depth guard), into
    # ``folded_slugs`` as a self-alias, and into the wikilink rewrite, which
    # would have repointed real inbound ``[[<uid>-<slug>]]`` links at a slug
    # no file owns. Both tests are kept: the slug one is still the primary
    # guard for a bare-slug target, and it is the only one that works for a
    # source path that no longer exists on disk.
    def _is_canonical(src: str) -> bool:
        return slugify(Path(src).stem) == target_slug or _same_file(
            Path(src), target_path
        )

    all_source_slugs = _source_slugs(pm.sources)
    canonical_slugs = {
        slugify(Path(src).stem) for src in pm.sources if _is_canonical(src)
    }
    canonical_slugs.add(target_slug)
    folded_slugs = [s for s in all_source_slugs if s not in canonical_slugs]
    folded_sources = [src for src in pm.sources if not _is_canonical(src)]

    # Step -1b (issue athenaeum#716): read every folded source's CURRENT
    # frontmatter up front (needed below for both the preflight and the
    # eventual tombstone stamp), and refuse if any is ALREADY a tombstone —
    # a tombstone's `folded_into` is set exactly once; re-pointing it at a
    # DIFFERENT canonical here would silently abandon its original fold
    # record and could manufacture a cycle this function has no way to
    # detect in general (the acyclic guarantee holds by construction only
    # because this is refused, not because it is checked exhaustively).
    source_texts: dict[str, str] = {}
    source_metas: dict[str, dict[str, Any]] = {}
    for src in folded_sources:
        src_path = Path(src)
        try:
            src_text = src_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            src_text = ""
        src_meta, _ = parse_frontmatter(src_text)
        if not isinstance(src_meta, dict):
            src_meta = {}
        source_texts[src] = src_text
        source_metas[src] = src_meta
        if is_tombstone(src_meta):
            return {
                "ok": False,
                "error_code": "fold_source_already_tombstoned",
                "message": (
                    f"{src_path} is already a tombstone (folded_into="
                    f"{tombstone_target(src_meta)!r}); refusing to re-fold "
                    "an already-folded page into a different canonical — "
                    "exactly one live canonical per fold set, and a "
                    "tombstone's folded_into is set once and never "
                    "overwritten."
                ),
            }

    # Step -1c (issue athenaeum#716): coordinate widening + the narrowing-
    # invariant refusal. Parse the draft's OWN frontmatter (it may carry
    # none at all) without writing anything to disk yet, compute the widest
    # coordinate covering {prior canonical, draft, every folded source} on
    # every separator dimension (reusing `_widen_dimension`, never a second
    # implementation), patch that into the draft's frontmatter, and THEN
    # verify the result is not narrower than any folded source — refusing
    # the fold outright (nothing written, no commit taken) if it is.
    draft_meta, draft_body_text = parse_frontmatter(pm.draft_merged_body)
    if not isinstance(draft_meta, dict):
        draft_meta = {}
    widened_draft_meta = dict(draft_meta)
    coordinates_widened: dict[str, Any] = {}
    for dimension in registry:
        if not dimension.separates:
            continue
        metas_in_order = [prior_meta, draft_meta] + [
            source_metas[s] for s in folded_sources
        ]
        widened_value = _widen_over_metas(dimension, metas_in_order)
        current_value = (
            parsed_coordinate(dimension, draft_meta)
            if dimension.kind == DimensionKind.INTERVAL
            else coordinate_value(dimension, draft_meta)
        )
        if widened_value != current_value:
            _write_coordinate(widened_draft_meta, dimension, widened_value)
            coordinates_widened[dimension.name] = _json_safe_coordinate(widened_value)

    for dimension in registry:
        if not dimension.separates:
            continue
        for src in folded_sources:
            if not _coordinate_is_at_least_as_wide(
                dimension, widened_draft_meta, source_metas[src]
            ):
                return {
                    "ok": False,
                    "error_code": "fold_narrows_coordinate",
                    "message": (
                        f"fold refused: canonical {target_slug!r}'s post-fold "
                        f"{dimension.name!r} coordinate would be narrower "
                        f"than folded source {src!r}'s — coordinates widen "
                        "on a fold, they never narrow (silent scope collapse "
                        "is a fold bug by definition). If these are meant to "
                        "diverge, specialize a new narrow claim instead of "
                        "narrowing the canonical."
                    ),
                }

    if widened_draft_meta != draft_meta:
        effective_draft_body = (
            render_frontmatter(widened_draft_meta) + draft_body_text
            if widened_draft_meta
            else draft_body_text
        )
    else:
        effective_draft_body = pm.draft_merged_body

    # --- Commit A: provenance snapshot, BEFORE any write below (issue
    # athenaeum#947). Stages exactly the target page (about to be
    # overwritten in step 1) and every folded-away source (about to be
    # deleted in step 5) with a SCOPED pathspec — never `git add -A` — so an
    # operator's OTHER pre-staged/modified content elsewhere in the
    # knowledge repo can never be swept into this commit under a misleading
    # "provenance snapshot" message (the exact concern
    # ``auto_memory_prune.apply_prune`` documents for its own commit).
    # Commits only when something is ACTUALLY staged (`git diff --cached
    # --quiet` returncode != 0) — the common case where the target/sources
    # are already fully committed from a prior run is a legitimate no-op,
    # not an error.
    repo_root_resolved = repo_root.resolve()
    snapshot_rel_paths: list[str] = []
    for p in (target_path, *[Path(s) for s in folded_sources]):
        try:
            snapshot_rel_paths.append(str(p.resolve().relative_to(repo_root_resolved)))
        except ValueError:
            # Outside repo_root -- cannot be captured by a git snapshot.
            # Step 5 below applies the identical relative_to() check before
            # deleting, so a path that fails here is also never deleted.
            continue
    if snapshot_rel_paths:
        _git(repo_root, "add", "--", *snapshot_rel_paths)
        staged = _git(repo_root, "diff", "--cached", "--quiet", "--", *snapshot_rel_paths)
        if staged.returncode != 0:
            _git(
                repo_root,
                "commit",
                "-m",
                f"librarian: fold provenance snapshot ({target_slug}) (athenaeum#947)",
                "--",
                *snapshot_rel_paths,
            )

    # Step 1 — write the merged (coordinate-widened) draft body to the
    # canonical target.
    atomic_write_text(target_path, effective_draft_body)

    # Step 3 — alias map, deduped. The draft body just written in step 1 may
    # carry its OWN frontmatter (a merge draft can legitimately open with
    # one) — that becomes the base we add aliases: onto, with the prior
    # target's aliases: carried forward first so a second fold accumulates
    # rather than resetting.
    target_text = target_path.read_text(encoding="utf-8")
    target_meta, target_body = parse_frontmatter(target_text)
    if not isinstance(target_meta, dict):
        target_meta = {}
    carried_meta = _add_aliases_to_frontmatter(
        target_meta, list(prior_aliases_list)
    )
    new_meta = _add_aliases_to_frontmatter(carried_meta, folded_slugs)
    if new_meta != target_meta:
        final_target_text = render_frontmatter(new_meta) + target_body
        atomic_write_text(target_path, final_target_text)
    else:
        final_target_text = target_text
    aliases_added = [s for s in folded_slugs if s not in existing_alias_slugs]

    # Issue athenaeum#716: the canonical's post-fold content hash, taken
    # HERE — after the step-1 body write and the step-3 alias write, not
    # before — is the comparison basis a later ``unfold`` (lane 716-C) uses
    # to decide direct-unfold vs queued-diff-proposal.
    canonical_content_hash = content_hash(final_target_text)

    # Step 4 — rewrite inbound wikilinks pointing at any folded slug.
    # ``touched`` collects the exact sibling paths modified (a-priori
    # unknown — could be any page under ``wiki_root``) so Commit B below
    # can scope its pathspec to them instead of guessing (issue athenaeum#947).
    # ``link_details`` collects the per-link (path, from_slug, to_slug) the
    # merge-provenance ledger needs to be reversal-sufficient (issue athenaeum#716).
    rewritten_touched: list[Path] = []
    link_details: list[dict[str, str]] = []
    links_rewritten = _rewrite_inbound_wikilinks(
        wiki_root,
        folded_slugs,
        target_slug,
        skip=target_path,
        touched=rewritten_touched,
        link_details=link_details,
    )

    # Step 5 — TOMBSTONE the old source wiki files (issue athenaeum#716 — "a
    # merge may destroy renderings, never observations"). Never a
    # ``git rm``, never a bare ``Path.unlink()``: the file stays on disk,
    # ``git add`` stages the IN-PLACE frontmatter edit regardless of
    # whether the source was already tracked (unlike a delete, a modify-or-
    # create add needs no tracked/untracked fallback branch at all).
    tombstoned_paths: list[str] = []
    tombstone_staged_rel: list[str] = []
    for src in folded_sources:
        src_path = Path(src)
        try:
            # Issue athenaeum#748: never tombstone a source whose resolved path
            # IS the canonical target page, even if it slipped past the
            # slug-based ``folded_sources`` filter above (e.g. a differently
            # -spelled path that resolves to the same file). The slug filter
            # is the primary guard; this path-equality check is defense in
            # depth so no code path can tombstone the page being folded into.
            if _same_file(src_path, target_path):
                continue
            if not src_path.is_file():
                continue
            try:
                rel_src = str(src_path.resolve().relative_to(repo_root_resolved))
            except ValueError:
                log.warning(
                    "pending_merges: folded source %s is outside git repo %s; "
                    "skipping tombstone (git-only modification cannot be "
                    "guaranteed)",
                    src_path,
                    repo_root,
                )
                continue
            stamped_meta = stamp_tombstone(source_metas[src], target_slug)
            _, src_body = parse_frontmatter(source_texts[src])
            atomic_write_text(src_path, render_frontmatter(stamped_meta) + src_body)
            add_result = _git(repo_root, "add", "--", rel_src)
            if add_result.returncode == 0:
                tombstone_staged_rel.append(rel_src)
            else:
                log.warning(
                    "pending_merges: git add failed for tombstoned source "
                    "%s (%s)",
                    src_path,
                    add_result.stderr.strip(),
                )
            tombstoned_paths.append(src)
        except OSError as exc:
            log.warning(
                "pending_merges: could not tombstone folded source %s: %s",
                src_path,
                exc,
            )

    # Step 6 — best-effort vector purge for the tombstoned slugs (unchanged
    # — this is what keeps a tombstone non-polluting on day one rather than
    # waiting for the next reindex to notice ``embedded: false``).
    _purge_vector_ids(
        [
            s
            for s in folded_slugs
            if any(slugify(Path(p).stem) == s for p in tombstoned_paths)
        ],
        cache_dir=cache_dir,
        search_backend=search_backend,
        embedding_model=embedding_model,
    )

    # --- Commit B: the fold itself, as its own commit (issue athenaeum#947).
    # `git add` the target page (rewritten in steps 1/3), any wikilink-
    # rewritten siblings (step 4), and every tombstoned source (step 5) —
    # each a SPECIFIC path, never a directory-wide or repo-wide add, so this
    # commit cannot silently absorb unrelated dirty state either.
    add_rel_paths = list(
        dict.fromkeys(
            [str(target_path.resolve().relative_to(repo_root_resolved))]
            + [
                str(p.resolve().relative_to(repo_root_resolved))
                for p in rewritten_touched
            ]
            + tombstone_staged_rel
        )
    )
    if add_rel_paths:
        _git(repo_root, "add", "--", *add_rel_paths)
    commit_b_rel_paths = add_rel_paths
    if commit_b_rel_paths:
        staged_b = _git(
            repo_root, "diff", "--cached", "--quiet", "--", *commit_b_rel_paths
        )
        if staged_b.returncode != 0:
            folded_slug_desc = ", ".join(folded_slugs) if folded_slugs else "(none)"
            _git(
                repo_root,
                "commit",
                "-m",
                f"librarian: fold {folded_slug_desc} into {target_slug} (athenaeum#947)",
                "--",
                *commit_b_rel_paths,
            )

    return {
        "ok": True,
        "folded_sources": tombstoned_paths,
        "link_details": link_details,
        "canonical_content_hash": canonical_content_hash,
        "coordinates_widened": coordinates_widened,
        "aliases_added": aliases_added,
        "links_rewritten": links_rewritten,
    }


def resolve_merge(
    merges_path: Path,
    merge_id: str,
    decision: Literal["approve", "reject"],
    note: str = "",
    *,
    wiki_root: Path | None = None,
    cache_dir: Path | None = None,
    search_backend: str | None = None,
    embedding_model: str | None = None,
    auto_applied: bool = False,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Mark a pending-merge block as resolved.

    Args:
        merges_path: Path to ``_pending_merges.md``.
        merge_id: Id returned by :func:`list_pending_merges`.
        auto_applied: Issue athenaeum#602. When ``True``, this ``"approve"`` is being
            finalized by the T2 reasoning tier's auto-finalize path, NOT by
            a human — the caller
            (:func:`athenaeum.reasoning_screens.t2_screen_merge_proposal`)
            is the ONLY production caller that ever passes ``True``. This
            reuses the existing approve-time fold/write mechanics byte for
            byte (no second write path); the only effect is two durable
            markers: a ``**Auto-applied**: true`` line in the resolved block
            (human-readable, in the wiki sidecar itself) and
            ``auto_applied: true`` on the provenance ledger record (queryable
            via ``athenaeum merges provenance``). Ignored (has no effect) on
            ``decision="reject"`` — there is no such thing as an
            "auto-applied reject". Defaults to ``False`` so every existing
            caller (the human MCP/CLI approve path) is byte-identical to
            before this parameter existed.
        decision: ``"approve"`` dispatches on the proposal's ``write_kind``
            (issue athenaeum#421 classification, issue athenaeum#425 write paths):

            - ``"create-merged"`` (unchanged behavior): writes
              ``wiki/<target-slug>.md`` (or under ``wiki_root`` when
              supplied) with ``draft_merged_body``. Fails closed with
              ``target_exists`` if the slug is already taken — including a
              MISCLASSIFIED create-kind proposal, as defense in depth.
            - ``"fold-into-existing"``: the target slug is the CANONICAL
              existing page; sources fold INTO it. Writes a coordinate-
              widened ``draft_merged_body`` to the existing target, rewrites
              every inbound ``[[old-slug]]`` wikilink under ``wiki_root`` to
              the canonical slug, adds the folded-away source slugs to the
              canonical page's ``aliases:`` frontmatter (deduped),
              TOMBSTONES the old source wiki files in place (issue athenaeum#716
              — ``status: folded`` / ``folded_into: <slug>`` / ``embedded:
              false``; never deleted), and (when ``cache_dir`` +
              ``search_backend="vector"`` are supplied) purges their
              vectors from the vector store. ``target_exists`` is
              unreachable here for a correctly-classified proposal — the
              precheck at proposal time (`_classify_merge_write_kind`)
              already confirmed the slug exists. Issue athenaeum#947: the
              tombstone step (and the target-page overwrite before it) is
              refused with ``no_git_repo`` — no file is touched, the
              checkbox is not flipped — unless ``wiki_root`` resolves
              (via ``git rev-parse --show-toplevel``) inside a git
              repository. The tombstone itself no longer NEEDS git to be
              recoverable (the page is never removed), but the gate is kept
              exactly as-is — it still protects the body overwrite and the
              inbound-link rewrite the same way it always has (README.md's
              recovery guarantee). When it does resolve, the fold lands as
              TWO commits: a provenance snapshot of the target + sources
              taken before any write, then the fold itself. This gate
              applies ONLY to ``fold-into-existing`` — ``create-merged``
              touches nothing else and is unaffected.

            Either way, on success a provenance record is appended (see
            :func:`athenaeum.provenance.record_merge_provenance`) naming
            the canonical slug, source paths, merge id, and write_kind.
            The source memories are NOT archived/deleted for
            ``create-merged`` — the human reviews the wiki write before any
            source change; ``fold-into-existing`` TOMBSTONES the (wiki-tree)
            source files in place (issue athenaeum#716 — never deleted) as part
            of consolidation, since the merge target already existed and
            review has already happened at approval time.

            ``"reject"`` flips the checkbox and writes an honest,
            non-directional ``merge_rejected_with:`` declaration into the
            first source memory (issue athenaeum#715 — NEVER a ``refines:``
            declaration; a rejection is not a specialization claim) so
            the detector's declared-pair short-circuit suppresses the
            pair on future runs via the distinct
            ``"declared-merge-rejection"`` rationale.
        note: Optional human note attached to the decision block.
        wiki_root: Optional wiki root override (defaults to
            ``merges_path.parent``).
        cache_dir: Optional search-index cache dir. When supplied together
            with ``search_backend="vector"``, a ``fold-into-existing``
            approve purges the tombstoned sources' vectors from the store
            (issue athenaeum#425 embedding hygiene). ``None`` (default) skips the
            purge — vector hygiene is opportunistic, never a hard
            dependency of resolving a merge.
        search_backend: The configured search backend name (``"vector"``
            enables the purge above; anything else, including ``None``,
            skips it).
        embedding_model: Embedding model name passed through to the vector
            backend purge call, matching the model the live index was
            built with (see :class:`athenaeum.search.VectorBackend`).
        registry: Dimension registry (issue athenaeum#716) used to decide
            which separator dimensions a fold widens / narrowing-checks.
            Defaults to :data:`athenaeum.dimensions.DEFAULT_REGISTRY` (the
            kernel-only registry), matching
            :func:`athenaeum.comparator.compare_pages`'s own default.

    Returns:
        ``{"ok": bool, "error_code": str | None, "message": str,
           "resolved_block": str | None}``. A ``fold-into-existing``
        approve additionally sets ``"folded_sources"`` (the tombstoned
        source paths), ``"aliases_added"`` (the new alias slugs recorded),
        and ``"links_rewritten"`` (the count of sibling wiki files whose
        inbound wikilinks were repointed).
    """
    if decision not in ("approve", "reject"):
        return {
            "ok": False,
            "error_code": "invalid_decision",
            "message": f"decision must be 'approve' or 'reject', got {decision!r}",
            "resolved_block": None,
        }
    if not merges_path.exists():
        return {
            "ok": False,
            "error_code": "file_missing",
            "message": f"pending merges file not found: {merges_path}",
            "resolved_block": None,
        }
    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": "no pending merge blocks in file",
            "resolved_block": None,
        }

    target_pm: PendingMerge | None = None
    resolved_text: str | None = None
    rewritten: list[str] = []
    for block_text in blocks:
        pm = _parse_block(block_text)
        if pm is None or pm.id != merge_id:
            rewritten.append(block_text)
            continue
        if pm.resolved:
            return {
                "ok": False,
                "error_code": "already_resolved",
                "message": f"merge {merge_id} already resolved",
                "resolved_block": None,
            }
        target_pm = pm
        resolved_text = _rewrite_block_resolved(
            block_text,
            decision,
            note,
            auto_applied=auto_applied and decision == "approve",
        )
        rewritten.append(resolved_text)

    if target_pm is None:
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": f"merge id not found: {merge_id}",
            "resolved_block": None,
        }

    warning: str | None = None
    extra_response: dict = {}
    # Issue athenaeum#716: the fold-specific ledger keys (`folded_sources`,
    # `aliases_added`, `links_rewritten` detail, `canonical_content_hash`,
    # `coordinates_widened`), forwarded to `record_merge_provenance` below.
    # Stays empty for `create-merged` and for `reject`, so neither write a
    # single one of these fold-only keys onto their ledger record.
    fold_ledger_extra: dict[str, Any] = {}

    # Apply the side-effect tied to the decision BEFORE flushing the file.
    if decision == "approve":
        root = wiki_root or merges_path.parent
        root.mkdir(parents=True, exist_ok=True)
        target_slug = slugify(target_pm.merge_target_name)
        # Issue athenaeum#1642: resolve the target by IDENTITY, through the
        # same :func:`resolve_target_page` the proposal-time
        # :func:`classify_write_kind` uses — never by filename shape alone.
        # A real corpus page lives at ``<uid>-<slug>.md``, so the old
        # ``root / f"{target_slug}.md"`` derivation found nothing for it:
        # the proposal classified ``create-merged`` and this branch then
        # wrote a duplicate page rather than folding. Sharing the resolver
        # is what makes the docstring invariant on ``classify_write_kind``
        # structural — a ``fold-into-existing`` proposal cannot later fail
        # ``fold_target_missing``, and a ``create-merged`` one cannot later
        # fail ``target_exists``, because both questions are now answered by
        # the same function over the same corpus.
        resolved_target = resolve_target_page(target_pm.merge_target_name, root)
        # ``None`` means no page owns the slug: the create path writes the
        # new page at the bare-slug filename, exactly as before.
        target_path = (
            resolved_target
            if resolved_target is not None
            else root / f"{target_slug}.md"
        )
        write_kind = target_pm.write_kind

        if write_kind == "fold-into-existing":
            # Issue athenaeum#748: re-verify the fold target actually exists
            # before taking the delete-sources path. ``write_pending_merge``
            # now derives ``write_kind`` and cannot produce a fold block for a
            # non-existent target, but a hand-edited / legacy sidecar block can
            # still carry a misclassified ``fold-into-existing``. Folding when
            # the target is absent would write the draft to a NEW page and then
            # delete every source — including the canonical page the fold was
            # meant to fold INTO (the 2026-08-02 incident). Fail closed with a
            # distinct error code instead of proceeding to the delete; do NOT
            # flip the checkbox (return before flushing ``rewritten``).
            if resolved_target is None:
                return {
                    "ok": False,
                    "error_code": "fold_target_missing",
                    "message": (
                        f"fold target {target_path} does not exist; refusing "
                        "to fold — a fold whose target is absent would delete "
                        "the source pages and create a new page. Reclassify "
                        "as create-merged (rename merge_target_name to the "
                        "canonical page's name)."
                    ),
                    "resolved_block": None,
                }
            # Issue athenaeum#947: removal must be git-only for recoverability
            # (README.md's "a bad merge is a `git revert` away" guarantee) —
            # mirrors ``auto_memory_prune.apply_prune``'s refuse-don't-degrade
            # gate. This fires BEFORE any mutation whatsoever (before the
            # draft body overwrites the target below) and must NOT flip the
            # checkbox, same shape as the ``fold_target_missing`` check above.
            # Scoped to ``fold-into-existing`` only — ``create-merged``
            # deletes nothing and is unaffected. See ``_find_git_repo``'s
            # docstring for why this asks git itself rather than checking
            # ``(knowledge_root / ".git").exists()`` like the other
            # git-gated destructive paths in this codebase.
            repo_root = _find_git_repo(root)
            if repo_root is None:
                return {
                    "ok": False,
                    "error_code": "no_git_repo",
                    "message": (
                        f"{root} is not inside a git repository; refusing to "
                        "fold — a fold-into-existing approve tombstones the "
                        "folded-away source pages in place, and that write "
                        "must stay git-only so the body overwrite and the "
                        "inbound-link rewrite it also performs stay "
                        "recoverable via `git revert` (README.md's recovery "
                        "guarantee). Initialize the knowledge root as a git "
                        "repo before approving this merge."
                    ),
                    "resolved_block": None,
                }
            fold_result = _apply_fold_into_existing(
                target_pm,
                target_path=target_path,
                target_slug=target_slug,
                wiki_root=root,
                repo_root=repo_root,
                cache_dir=cache_dir,
                search_backend=search_backend,
                embedding_model=embedding_model,
                registry=registry,
            )
            if not fold_result["ok"]:
                return {
                    "ok": False,
                    "error_code": fold_result["error_code"],
                    "message": fold_result["message"],
                    "resolved_block": None,
                }
            extra_response = {
                "folded_sources": fold_result["folded_sources"],
                "aliases_added": fold_result["aliases_added"],
                "links_rewritten": fold_result["links_rewritten"],
            }
            fold_ledger_extra = {
                "folded_sources": fold_result["folded_sources"],
                "aliases_added": fold_result["aliases_added"],
                "links_rewritten": fold_result["link_details"],
                "canonical_content_hash": fold_result["canonical_content_hash"],
                "coordinates_widened": fold_result["coordinates_widened"],
            }
        else:
            # ``create-merged`` path — UNCHANGED behavior. A misclassified
            # create-kind proposal that hits an existing slug still fails
            # closed here (defense in depth): the athenaeum#421 precheck should have
            # classified it fold-into-existing, but a stale/hand-edited
            # block's write_kind is not trusted blindly.
            if resolved_target is not None:
                # Fail closed: do NOT flip the checkbox; the human must
                # rename the merge_target_name or resolve the existing
                # wiki entry.
                return {
                    "ok": False,
                    "error_code": "target_exists",
                    "message": (
                        f"{target_path} already exists; rename merge_target_name "
                        "or resolve the existing memory first"
                    ),
                    "resolved_block": None,
                }
            atomic_write_text(target_path, target_pm.draft_merged_body)

        record_merge_provenance(
            root,
            merge_id=target_pm.id,
            write_kind=write_kind,
            canonical_slug=target_slug,
            source_paths=list(target_pm.sources),
            auto_applied=auto_applied,
            **fold_ledger_extra,
        )
    elif decision == "reject" and len(target_pm.sources) >= 2:
        # Issue athenaeum#715 (restating athenaeum#658's D3): a rejection means "these
        # two are NOT the same claim" — a completely different assertion
        # from "A refines B", so it must never be recorded as a
        # fabricated `refines:` declaration. Instead write an honest,
        # non-directional `merge_rejected_with:` record into the first
        # source memory naming the second. merge.py's
        # `_declared_relationship` reads this field and returns the
        # distinct `"declared-merge-rejection"` rationale, which keeps
        # Lane 1 / athenaeum#167's declared-pair short-circuit suppressing this
        # pair on future detector runs with NO config gate — deleting the
        # marker outright without a replacement would make every rejected
        # pair re-propose on every nightly run, which the issue's
        # Definition of Done forbids.
        #
        # Deliberately NOT plumbed into the verdict ledger here:
        # `verdicts.append_verdict` requires a `RunLock` that
        # `resolve_merge` does not hold, and this module has no ledger
        # integration at all today. Writing a `distinct` verdict from this
        # path is a materially larger change than this fix and properly
        # belongs to the comparator's own `athenaeum merges recompare`
        # command, which already exists — do not re-derive this boundary
        # in a future pass without re-reading this comment.
        #
        # Migration note: existing corpus pages may already carry
        # fabricated `refines:` edges from rejections recorded before this
        # change. This stops NEW ones; it does not rewrite any existing
        # store (out of scope here).
        src_a = Path(target_pm.sources[0])
        src_b = Path(target_pm.sources[1])
        # Prefer source B's frontmatter `name:` so renames / custom slugs
        # round-trip; fall back to the filename stem (minus conventional
        # prefix) only when the frontmatter is missing/unreadable.
        other_name: str | None = None
        if src_b.is_file():
            try:
                b_text = src_b.read_text(encoding="utf-8")
                b_meta, _ = parse_frontmatter(b_text)
                if isinstance(b_meta, dict):
                    raw_name = b_meta.get("name")
                    if isinstance(raw_name, str) and raw_name.strip():
                        other_name = raw_name.strip()
            except (OSError, UnicodeDecodeError):
                other_name = None
        if other_name is None:
            other_stem = src_b.stem
            for prefix in (
                "feedback_",
                "project_",
                "reference_",
                "user_",
                "recall_",
            ):
                if other_stem.startswith(prefix):
                    other_stem = other_stem[len(prefix) :]
                    break
            other_name = other_stem
        if not src_a.is_file():
            warning = (
                "merge_rejection_write_failed: source A unavailable or unwritable; "
                "merge will re-propose on next run"
            )
        else:
            try:
                _add_merge_rejection_declaration(src_a, other_name)
            except OSError:
                warning = (
                    "merge_rejection_write_failed: source A unavailable or "
                    "unwritable; merge will re-propose on next run"
                )

    primary_parts = ["# Pending Merges", *rewritten]
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(merges_path, primary_body)

    response: dict = {
        "ok": True,
        "error_code": None,
        "message": "ok",
        "resolved_block": resolved_text,
    }
    response.update(extra_response)
    if warning is not None:
        response["warning"] = warning
    return response


def ingest_resolved_merges(merges_path: Path) -> int:
    """Move resolved (``[x]``) blocks from primary file to archive.

    Same shape as :func:`athenaeum.answers.ingest_answers` for the
    questions sidecar. Idempotent. Returns the number of merges archived
    on this run.

    Also COMPACTS the primary file every run: it is rewritten from the
    blocks :func:`_split_blocks` still recognizes as canonical merge
    blocks, which drops any orphan ``## From`` fragments a broken draft
    fence leaked in an earlier version (issue athenaeum#394). The recomposed form
    is stable — re-splitting it yields the same blocks — so once the
    backlog has drained the file stops changing and no needless rewrite
    happens. This is what makes the 13 MB regressed sidecar shrink on the
    next run instead of only when a human happens to resolve a merge.
    """
    if not merges_path.exists():
        return 0
    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)

    archive_path = merges_path.parent / "_pending_merges_archive.md"
    iso_ts = now_iso()

    remaining: list[str] = []
    archived: list[str] = []
    for block_text in blocks:
        pm = _parse_block(block_text)
        if pm is None or not pm.resolved:
            remaining.append(block_text)
            continue
        archived.append(f"{block_text}\n\n**Archived**: {iso_ts}\n")

    # Recompose the primary file from recognized blocks. Any leaked orphan
    # ``## From`` fragment that _split_blocks no longer treats as a block is
    # dropped here, draining the sidecar (issue athenaeum#394). Compact even when
    # nothing was archived this run, but only actually write when the bytes
    # would change, so a clean file is left untouched.
    primary_parts = ["# Pending Merges", *remaining]
    new_primary = "\n\n---\n\n".join(primary_parts) + "\n"
    if not archived:
        if new_primary != text:
            atomic_write_text(merges_path, new_primary)
            log.info(
                "pending_merges: compacted sidecar %s (%d -> %d bytes), "
                "no resolved blocks to archive",
                merges_path.name,
                len(text),
                len(new_primary),
            )
        return 0

    atomic_write_text(merges_path, new_primary)

    existing_archive = ""
    if archive_path.exists():
        existing_archive = archive_path.read_text(encoding="utf-8")
    new_section = "\n\n---\n\n".join(archived)
    if existing_archive.strip():
        if existing_archive.startswith("# Archived Merges"):
            _, _, rest = existing_archive.partition("\n")
            combined = (
                "# Archived Merges\n\n" + new_section + "\n\n---\n\n" + rest.lstrip()
            )
        else:
            combined = (
                "# Archived Merges\n\n"
                + new_section
                + "\n\n---\n\n"
                + existing_archive.lstrip()
            )
    else:
        combined = "# Archived Merges\n\n" + new_section + "\n"
    atomic_write_text(archive_path, combined)
    return len(archived)


@dataclass
class RetiredProposal:
    """One unresolved block the current suppression gate would now retire."""

    id: str
    merge_target_name: str
    n_sources: int
    confidence: float
    reason: str


@dataclass
class RevalidationResult:
    """Outcome of :func:`revalidate_pending_merges`.

    ``retired`` lists the unresolved blocks the CURRENT gate would suppress;
    ``kept`` counts the blocks left in the primary file (unresolved-and-legal
    + resolved + unparseable). ``applied`` is True only when the sweep wrote.
    """

    retired: list[RetiredProposal]
    kept: int
    applied: bool


def revalidate_pending_merges(
    merges_path: Path,
    *,
    config: dict[str, Any] | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> RevalidationResult:
    """Re-validate EXISTING unresolved ``_pending_merges.md`` blocks against
    the CURRENT suppression gate and archive ones that now fail it (issue athenaeum#481).

    athenaeum#480 closed the write-path bypass, so no NEW degenerate over-cluster
    proposal can be appended. This is the complement: it retires entries that
    were queued BEFORE the athenaeum#400/#421 gate tightened — the class the pipeline
    would never propose today (the reported 1,711/1,746-source
    ``merge-workflow-pattern`` and 16-source ``contact-contacts-wiki``, and the
    font-name / year-range / hash-fragment over-clusters). Without it, a
    withdrawn-and-regrown queue simply re-accumulates that junk until a human
    does a manual purge.

    Non-destructive by construction:

    * A retired block is MOVED to ``_pending_merges_archive.md`` with the gate
      reason recorded — never deleted — so a surprising purge is auditable.
    * It writes NO ``refines:`` suppression on the source pages (the athenaeum#437
      trap). Retiring a stale proposal leaves the pair free to be re-proposed
      if a future, healthy pipeline would emit it — this is a queue-hygiene
      sweep, NOT a per-pair rejection and NOT a queue-clearing tool.
    * Genuine (non-suppressed) unresolved blocks, already-resolved blocks, and
      any block that fails to parse are left byte-for-byte untouched.

    Only the gate arms whose inputs survive in the STORED block are applied:
    the size cap (``n_sources`` = number of listed sources) and the
    resolver-confidence floor. The cohesion / complete-linkage arms need the
    clustering-time pairwise-similarity data that a block does not persist, so
    they stay at their permissive defaults — the sweep only ever retires what
    it can prove stale from the block alone. The size cap is exactly what the
    reported over-cluster evidence trips, so this covers the driving case.

    Dry-run by default (reports what WOULD be retired, writes nothing); pass
    ``apply=True`` to write. ``now`` is injectable for deterministic tests.
    """
    result = RevalidationResult(retired=[], kept=0, applied=False)
    if not merges_path.exists():
        return result

    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)

    ts = now_iso(now)
    remaining: list[str] = []
    retired_blocks: list[str] = []

    for block_text in blocks:
        pm = _parse_block(block_text)
        # Only unresolved, parseable blocks are candidates. Everything else
        # (resolved, or unparseable) is preserved exactly.
        if pm is None or pm.resolved:
            remaining.append(block_text)
            continue
        reason = _merge_proposal_suppression_reason(
            n_sources=len(pm.sources),
            confidence=pm.confidence,
            config=config,
        )
        if reason is None:
            remaining.append(block_text)
            continue
        result.retired.append(
            RetiredProposal(
                id=pm.id,
                merge_target_name=pm.merge_target_name,
                n_sources=len(pm.sources),
                confidence=pm.confidence,
                reason=reason,
            )
        )
        retired_blocks.append(
            f"{block_text}\n\n**Retired**: {ts} — gate: {reason}\n"
        )

    result.kept = len(remaining)

    if not apply or not retired_blocks:
        return result

    # --- apply: rewrite primary (retired blocks removed) + append archive ---
    primary_parts = ["# Pending Merges", *remaining]
    new_primary = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(merges_path, new_primary)

    archive_path = merges_path.parent / "_pending_merges_archive.md"
    new_section = "\n\n---\n\n".join(retired_blocks)
    existing_archive = ""
    if archive_path.exists():
        existing_archive = archive_path.read_text(encoding="utf-8")
    if existing_archive.strip():
        if existing_archive.startswith("# Archived Merges"):
            _, _, rest = existing_archive.partition("\n")
            combined = (
                "# Archived Merges\n\n" + new_section + "\n\n---\n\n" + rest.lstrip()
            )
        else:
            combined = (
                "# Archived Merges\n\n"
                + new_section
                + "\n\n---\n\n"
                + existing_archive.lstrip()
            )
    else:
        combined = "# Archived Merges\n\n" + new_section + "\n"
    atomic_write_text(archive_path, combined)

    result.applied = True
    log.info(
        "pending_merges: revalidation retired %d stale proposal(s) to %s "
        "(current suppression gate)",
        len(retired_blocks),
        archive_path.name,
    )
    return result


@dataclass
class WithdrawnMergeProposal:
    """One unresolved ``_pending_merges.md`` block withdrawn because it
    references a page a caller is retiring (issue athenaeum#1625)."""

    id: str
    merge_target_name: str
    reason: str


@dataclass
class WithdrawalResult:
    """Outcome of :func:`withdraw_pending_merges_for_retired_pages`."""

    withdrawn: list[WithdrawnMergeProposal]
    kept: int
    applied: bool


def withdraw_pending_merges_for_retired_pages(
    merges_path: Path,
    retired_pages: list[Path],
    *,
    reason: str,
    apply: bool = False,
    now: datetime | None = None,
) -> WithdrawalResult:
    """Withdraw/archive every unresolved block whose sources or target
    reference a page in *retired_pages* (issue athenaeum#1625, generic
    wiki-page retirement command).

    Mirrors :func:`revalidate_pending_merges`'s dry-run-by-default /
    ``apply=True`` split and its non-destructive "move to
    ``_pending_merges_archive.md``, never delete" discipline — see that
    function's docstring for why. The only difference is WHAT makes a
    block a candidate: here it is "references a page this run is
    retiring", not "fails the current merge-suppression gate".

    A block is withdrawn when either:

    - one of its ``**Sources**:`` entries names a retired page (matched by
      filename, since a source may be recorded as an absolute or a
      relative path), or
    - its merge target's slug (:func:`athenaeum.models.slugify` of
      ``merge_target_name``) equals a retired page's filename stem — i.e.
      the proposal would fold/create INTO a page that is being retired.

    Already-resolved blocks and unparseable blocks are left byte-for-byte
    untouched, same as :func:`revalidate_pending_merges`. Dry-run by
    default (reports what WOULD be withdrawn, writes nothing); pass
    ``apply=True`` to write.
    """
    result = WithdrawalResult(withdrawn=[], kept=0, applied=False)
    if not merges_path.exists():
        return result

    retired_names = {p.name for p in retired_pages}
    retired_stems = {p.stem for p in retired_pages}

    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)

    ts = now_iso(now)
    remaining: list[str] = []
    withdrawn_blocks: list[str] = []

    for block_text in blocks:
        pm = _parse_block(block_text)
        if pm is None or pm.resolved:
            remaining.append(block_text)
            continue

        hit_source = next(
            (s for s in pm.sources if Path(s).name in retired_names), None
        )
        target_retired = slugify(pm.merge_target_name) in retired_stems

        if hit_source is None and not target_retired:
            remaining.append(block_text)
            continue

        if target_retired:
            why = f"target page {slugify(pm.merge_target_name)!r} was retired"
        else:
            assert hit_source is not None  # the `continue` above ruled out both-None
            why = f"source {Path(hit_source).name!r} was retired"
        full_reason = f"{why}: {reason}" if reason else why

        result.withdrawn.append(
            WithdrawnMergeProposal(
                id=pm.id, merge_target_name=pm.merge_target_name, reason=full_reason
            )
        )
        withdrawn_blocks.append(f"{block_text}\n\n**Retired**: {ts} — {full_reason}\n")

    result.kept = len(remaining)

    if not apply or not withdrawn_blocks:
        return result

    # --- apply: rewrite primary (withdrawn blocks removed) + append archive,
    # mirroring `revalidate_pending_merges`'s file-writing shape exactly.
    primary_parts = ["# Pending Merges", *remaining]
    new_primary = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(merges_path, new_primary)

    archive_path = merges_path.parent / "_pending_merges_archive.md"
    new_section = "\n\n---\n\n".join(withdrawn_blocks)
    existing_archive = ""
    if archive_path.exists():
        existing_archive = archive_path.read_text(encoding="utf-8")
    if existing_archive.strip():
        if existing_archive.startswith("# Archived Merges"):
            _, _, rest = existing_archive.partition("\n")
            combined = (
                "# Archived Merges\n\n" + new_section + "\n\n---\n\n" + rest.lstrip()
            )
        else:
            combined = (
                "# Archived Merges\n\n"
                + new_section
                + "\n\n---\n\n"
                + existing_archive.lstrip()
            )
    else:
        combined = "# Archived Merges\n\n" + new_section + "\n"
    atomic_write_text(archive_path, combined)

    result.applied = True
    log.info(
        "pending_merges: withdrew %d proposal(s) referencing a retired page to %s",
        len(withdrawn_blocks),
        archive_path.name,
    )
    return result
