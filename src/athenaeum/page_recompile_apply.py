# SPDX-License-Identifier: Apache-2.0
"""Page recompile: the applier for structure-only plans (issue athenaeum#2540,
slice S4 of the page-recompile design, athenaeum#2525).

:mod:`athenaeum.page_recompile` (S1, athenaeum#2534) segments a page into
addressable :class:`~athenaeum.page_recompile.Unit` objects.
:mod:`athenaeum.page_recompile_plan` (S3, athenaeum#2537) is the shadow-only
planner: it labels, groups, routes and summarises, validates every decision
in code, and appends a :class:`~athenaeum.page_recompile_plan.RecompilePlan`
to the ledger -- but writes no page. THIS module is the write side for a
``structure-only`` plan (:data:`athenaeum.page_recompile_plan.SCOPE_STRUCTURE_ONLY`):
it re-reads the live page, refuses a plan whose pinned content hash no
longer matches (AC1), re-validates every decision in code a second time
(never trusting an already-``valid`` plan blindly -- see
:func:`compute_apply` for why), computes every output page in memory
FIRST, then writes.

**Operator ruling this module exists to carry out (athenaeum#2525).** A split
page stays a REAL parent, with a summary and its identity intact, with
sub-pages beneath it linked both ways -- it must NOT degrade into an empty
link hub. The pass MAY merge near-duplicate sections, with every citation
and footnote from the merged originals surviving and the originals
reachable through the undo snapshot. This is the fix for the retired
reactive split's (``tiers._perform_oversize_page_split``, deleted by issue
athenaeum#2550) two defects named in the issue: that function replaced the
parent body with a fixed notice plus a link list, and it sliced by
heading offset so a trailing footnote block landed only on the last
child.

**Compute-first, write-last -- the same discipline the retired reactive
split followed.** Every sub-page's
full rendered text, and the parent's full new text, are built in memory by
:func:`compute_apply` before any file on disk is touched. New sub-pages are
written first (each via :func:`athenaeum.atomic_io.atomic_write_text`,
itself torn-write-safe); the parent is written LAST, only once every
sub-page has landed.

**Journal (AC10).** A SIGKILL between two atomic writes is not something a
``try``/``except`` in this process can ever observe -- there is no
``finally`` to run. :func:`write_apply` writes a small JSON journal
(:data:`JOURNAL_FILENAME`) to *wiki_root* BEFORE the first sub-page write,
naming every sub-page path about to be created and the parent's own
pre-image content hash (the plan's already-pinned
:attr:`~athenaeum.page_recompile_plan.RecompilePlan.content_hash`). The
journal is removed immediately after the parent write succeeds.
:func:`rollback_stale_journal` -- run at the START of every apply, per
AC10 -- reads a leftover journal and asks exactly one question: does the
parent page's CURRENT body hash still equal the journal's recorded
pre-image hash? If yes, the parent was never rewritten (the process died
before or during the sub-page writes), so every listed sub-page path is a
pure addition and is safely unlinked. If no, the parent WAS rewritten (the
process died after the parent write but before the journal was removed) --
the sub-pages it now links to must NOT be deleted; the journal is simply
cleared.

**Ledger and snapshot (AC9).** The ledger (same file
:mod:`athenaeum.page_recompile_plan` already owns,
:func:`~athenaeum.page_recompile_plan.default_page_recompile_ledger_path`)
gains one new record :data:`APPLY_KIND` per successful apply: pre/post
content hash and the git blob id of the pre-image for every touched page,
the created uids, which units landed on which destination, and -- per
merged group -- the original units' ids and hashes. :func:`write_apply`
makes exactly ONE :meth:`athenaeum.store.FilesystemStore.snapshot` commit of
its own, after the ledger append, so a merged section's originals (no
longer live on any page) are reachable from that commit's parent.

**The pre-image blob id names the page's RAW on-disk bytes** (issue
athenaeum#2541), not a re-render of its parsed frontmatter -- see
:attr:`ComputedApply.parent_pre_text` for what went wrong when it did.
One residual limit remains and is deliberate: a blob id only RESOLVES if
those bytes were ever committed, and this function snapshots AFTER its
write, so a page that no earlier snapshot ever captured has its pre-image
in no git object at all. :mod:`athenaeum.page_recompile_undo` treats that
exactly like a page edited since the apply -- it writes nothing and queues
a decision item -- which is correct conservative behaviour, not a gap to
code around here.

**Cross-page actions, added by issue athenaeum#2548 (slice S7).** This
module was structure-only: it refused any plan whose
:attr:`~athenaeum.page_recompile_plan.RecompilePlan.scope` was not
:data:`~athenaeum.page_recompile_plan.SCOPE_STRUCTURE_ONLY`. It now also
carries out the four cross-page actions and an alias move, under one extra
condition the structure-only path does not have: an **approved
``page-recompile`` decision item**. :func:`_cross_page_approval_refusal` is
that gate, and it consults no mode -- AC8 requires the refusal to hold in
every mode, ``auto`` included.

The four, and the one AC that shapes each:

* ``move:<uid>`` -- the units are inserted VERBATIM on the target before
  its footnote block, under their own headings, with no wrapper heading and
  no edit to anything already there; their footnote definitions travel with
  them, renumbered on collision (AC1, :func:`_build_relocated_body` /
  :func:`_insert_relocated`). The move is REFUSED when the target's read
  scope is wider than the source's (AC2, :func:`access_widening_reason`),
  and is never refused for SIZE -- a target that ends up over the threshold
  is flagged ``needs_split`` and becomes a candidate instead (AC4).
* ``standalone:<group>`` -- a new page with a new uid and a name and type
  validated against the deployment schema, with NO relation to the source
  page (AC5). A proposed name that already resolves is converted to a
  ``move`` by the planner
  (:func:`athenaeum.page_recompile_plan.resolve_standalone_groups`) and
  refused here, never converted a second time.
* ``attach:<uid>`` / ``attach-self-under:<uid>`` -- the relation is set
  through :func:`athenaeum.page_relations.set_relation` with
  ``via="attach"``, which changes no body text beyond the breadcrumb and
  the Sub-pages rendering, and never renames the attached page (AC6).
* ``move-alias:<uid>:<alias>`` -- an alias leaves the parent ONLY when the
  plan lists it (AC12).

A unit that carries no citation and lands on another page gets one added
relocation footnote naming the page it came from and the plan id (AC3,
:func:`relocation_note`); a unit that already cites something gets nothing
added.

**Out of scope (per both issues).** Undo (:mod:`athenaeum.page_recompile_undo`),
the nightly phase, auto-apply, renaming an attached page, and rewriting a
target page's existing content.

**One residual limit, deliberately not closed here.** The AC10 journal
covers CREATED pages: a process killed between two writes has its new
pages unlinked by :func:`rollback_stale_journal`. An existing move target
is not a pure addition, so the journal cannot reverse it the same way --
within the process :func:`write_apply` restores every already-written
target from :attr:`TargetPageEdit.pre_text` if a later write raises, but a
SIGKILL has no such moment. The visible consequence is the conservative
one: the target keeps its moved units while the parent still has them too,
which the next scan sees as duplication and the apply's own snapshot
commit can revert wholesale. Recording pre-image TEXT in the journal would
close it and is a bigger question than this slice should settle (it would
put page bodies in a sidecar file).

Layering: L4 domain/pipeline, the same layer as its siblings
:mod:`athenaeum.page_recompile` and :mod:`athenaeum.page_recompile_plan`
(same-layer imports are permitted by ``tests/test_layer_boundary.py``).
Imports :mod:`athenaeum.page_relations` (L3) for the relation primitives
this module reuses rather than duplicates, :mod:`athenaeum.config` (L2),
:mod:`athenaeum.atomic_io`, :mod:`athenaeum.footnote_markers` and
:mod:`athenaeum.models` (all L1/L0), and :mod:`athenaeum.store` (L0/L1) for
:class:`~athenaeum.store.FilesystemStore`. Issue athenaeum#2550 AC6 added a
function-local import of :mod:`athenaeum.answers` and :mod:`athenaeum.tiers`
(both L4, same-layer) inside :func:`_archive_oversize_notices_for_applied_plan`
-- the oversize-family notice archived there is matched/removed with the
same primitives :func:`~athenaeum.tiers.tier4_escalate`'s own dedup and
:func:`~athenaeum.tiers.close_moot_notices` already use, rather than a
third parallel implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_page_recompile_merge_near_duplicates,
    resolve_page_recompile_min_parent_chars,
    resolve_page_size_threshold_chars,
)
from athenaeum.footnote_markers import FOOTNOTE_DEF_RE, INLINE_MARKER_RE, parse_footnote_definitions
from athenaeum.models import (
    EntityIndex,
    TokenUsage,
    WikiEntity,
    effective_audience,
    generate_uid,
    parse_frontmatter,
    render_frontmatter,
)
from athenaeum.page_recompile import (
    SegmentResult,
    check_conservation,
    check_marker_resolution,
    segment_page,
)
from athenaeum.page_recompile_plan import (
    PAGE_ACTIONS,
    PAGE_RECOMPILE_LEDGER_VERSION,
    SCOPE_STRUCTURE_ONLY,
    MergedSection,
    PlanGroup,
    PlanIssue,
    RecompilePlan,
    _append_record,
    default_page_recompile_ledger_path,
    find_plan_record,
    normalise_unit_text,
    page_content_hash,
    parse_alias_move,
    read_page_recompile_ledger,
    split_disposition,
    split_summary_sentences,
    verify_summary,
)
from athenaeum.page_recompile_queue import (
    QUEUE_KIND,
    VERDICT_APPROVE,
    VERDICT_APPROVE_STRUCTURE_ONLY,
)
from athenaeum.page_relations import (
    ROLE_CHILD,
    ChildRef,
    RelationError,
    _ancestor_uids,
    _children_display_rows,
    _index_pages_by_uid,
    _is_child_relation_row,
    _read_meta,
    _related_rows,
    apply_breadcrumb,
    apply_sub_pages_section,
    is_inactive_relation_target,
    read_relations,
    set_relation,
)
from athenaeum.screening import more_restrictive
from athenaeum.store import FilesystemStore, now_iso

# ---------------------------------------------------------------------------
# The ledger (AC9): one new record kind in the SAME file page_recompile_plan
# already owns.
# ---------------------------------------------------------------------------

#: Record kind for a successfully-applied plan (AC9), a sibling of
#: :data:`athenaeum.page_recompile_plan.LABEL_KIND` /
#: :data:`athenaeum.page_recompile_plan.PLAN_KIND` in the same append-only file.
APPLY_KIND = "apply"

#: Sidecar journal filename under ``wiki_root`` (AC10). Distinct from the
#: ledger (``_page_recompiles.jsonl``): the journal is a transient,
#: removed-on-success marker, never an audit record.
JOURNAL_FILENAME = "_page_recompile_apply.journal.json"


def _journal_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / JOURNAL_FILENAME


def _git_blob_id(content: bytes) -> str:
    """The git blob sha a ``git add`` of *content* would produce (AC9).

    Computed directly from the standard git object-hashing algorithm
    (``sha1("blob {len}\\0" + content)``) rather than shelling out to
    ``git hash-object`` -- deterministic, works even when *wiki_root* is not
    (or is not yet) a git repository, and needs no subprocess for a value
    this module only ever reads back out of its own ledger record.
    """
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _write_journal(
    wiki_root: Path,
    *,
    plan_id: str,
    uid: str,
    parent_path: Path,
    pre_content_hash: str,
    new_paths: Sequence[Path],
) -> None:
    payload = {
        "plan_id": plan_id,
        "uid": uid,
        "parent_path": str(parent_path),
        "pre_content_hash": pre_content_hash,
        "new_paths": [str(p) for p in new_paths],
    }
    atomic_write_text(_journal_path(wiki_root), json.dumps(payload, sort_keys=True) + "\n")


def _remove_journal(wiki_root: Path) -> None:
    try:
        _journal_path(wiki_root).unlink()
    except OSError:
        pass


def rollback_stale_journal(wiki_root: Path) -> dict[str, Any] | None:
    """Roll back a journal left behind by a killed process (AC10).

    Run at the start of every apply (and safe to run at the start of any
    other recompile entry point too -- it is a no-op when no journal
    exists). See the module docstring for the "did the parent actually get
    rewritten" question this answers before deciding whether the listed
    sub-page paths are safe to delete.

    Returns the journal's parsed payload (for logging), or ``None`` when
    there was no journal, or it could not be read as JSON (in which case it
    is still removed -- a journal this module cannot interpret must not
    block every future apply forever).
    """
    path = _journal_path(wiki_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    data = raw if isinstance(raw, dict) else None

    parent_rewritten = False
    if data is not None:
        parent_path_str = data.get("parent_path")
        pre_hash = data.get("pre_content_hash")
        if isinstance(parent_path_str, str) and isinstance(pre_hash, str):
            try:
                text = Path(parent_path_str).read_text(encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                _, body = parse_frontmatter(text)
                if page_content_hash(body) != pre_hash:
                    parent_rewritten = True

    if data is not None and not parent_rewritten:
        for raw_path in data.get("new_paths", []):
            if not isinstance(raw_path, str):
                continue
            try:
                Path(raw_path).unlink()
            except OSError:
                pass

    try:
        path.unlink()
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------
# Pure text helpers: joining relocated units, heading renormalisation
# (AC5's one permitted content edit besides footnote renumbering).
# ---------------------------------------------------------------------------


def _join_units(texts: Iterable[str]) -> str:
    """Concatenate *texts* with at least one blank line between pieces.

    Never alters a piece's own bytes (AC5: "byte-for-byte copies") -- it only
    INSERTS a separator between pieces that do not already end in a blank
    line, which relocated content needs regardless of whether its original
    neighbour is still adjacent on the new page.
    """
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        out.append(text)
        if not text.endswith("\n\n"):
            out.append("\n" if text.endswith("\n") else "\n\n")
    return "".join(out)


#: A top-level ATX heading line, anchored like
#: ``athenaeum.tiers._SECTION_HEADING_RE`` (the same shape the segmenter's
#: own heading grouping already keys on).
_LEADING_HEADING_RE = re.compile(r"^(#{1,6})([ \t])")


def renormalise_heading(text: str, *, target_level: int = 2) -> str:
    """Re-level every heading line in *text* so its FIRST heading becomes
    ``target_level`` (AC5's one permitted structural edit besides footnote
    renumbering), shifting any deeper heading in the same unit by the same
    delta. A no-op when *text* carries no leading heading, or its leading
    heading is already at *target_level*.
    """
    match = _LEADING_HEADING_RE.match(text)
    if match is None:
        return text
    current = len(match.group(1))
    if current == target_level:
        return text
    delta = target_level - current

    def _shift(line_match: "re.Match[str]") -> str:
        depth = len(line_match.group(1))
        new_depth = max(1, min(6, depth + delta))
        return "#" * new_depth + line_match.group(2)

    return re.sub(r"^(#{1,6})([ \t])", _shift, text, flags=re.MULTILINE)


def _render_footnote_block(labels: Iterable[str], definitions: Mapping[str, str]) -> str:
    """``[^label]: text`` lines for *labels* found in *definitions*, sorted.

    AC6: every definition's TEXT is copied unchanged from the original --
    this never synthesises or edits a definition, only selects which already
    -verbatim definitions land on one destination page.
    """
    ordered = sorted(label for label in set(labels) if label in definitions)
    if not ordered:
        return ""
    return "\n".join(f"[^{label}]: {definitions[label]}" for label in ordered) + "\n"


def _first_colliding_label(pairs: Iterable[tuple[str, str]]) -> str | None:
    """The first label in *pairs* whose text disagrees with an earlier
    occurrence of the SAME label, or ``None`` (AC5's footnote-label
    renumbering-on-collision clause).

    **Why renumbering itself is not implemented, and why this check still
    is (athenaeum#2540).** A structure-only plan draws every unit -- and
    therefore every footnote definition -- from exactly ONE source page,
    whose label namespace :func:`athenaeum.footnote_markers.parse_footnote_definitions`
    has ALREADY deduped to unique ("a repeated label keeps the FIRST
    definition", that function's own docstring). Two pieces landing on one
    destination can therefore never legitimately disagree about what a
    shared label means -- renumbering code written today would be dead
    code, never exercised by any input this applier can receive. AC5
    *permits* renumbering as an allowed edit; it does not mandate writing
    it, and cross-page moves (where a SECOND source page's own label
    namespace could genuinely collide with this one) are explicitly out of
    scope for this issue.

    What this function converts is "reasoned unreachable" into "enforced
    unreachable": it is wired into every destination's footnote assembly
    (:func:`compute_apply`) and ACTUALLY RUNS on every apply, so if a
    future slice adds cross-page moves without first implementing
    renumbering, this starts firing immediately -- loudly refusing the
    plan with no write -- instead of the destination silently keeping
    whichever lookup happened to run last and shipping two different
    claims under one label. A slice that adds cross-page moves must
    implement real renumbering (:func:`athenaeum.page_decompose._renumber`
    is the model -- rewrite every colliding label to a free one on that
    destination) rather than relying on this assertion to keep passing.
    """
    seen: dict[str, str] = {}
    for label, text in pairs:
        if label in seen:
            if seen[label] != text:
                return label
            continue
        seen[label] = text
    return None


# ---------------------------------------------------------------------------
# Cross-page text and access primitives (issue athenaeum#2548, slice S7).
# ---------------------------------------------------------------------------


def _label_namespace(body: str) -> set[str]:
    """Every footnote label *body* already occupies -- definitions AND bare
    inline refs alike.

    Same rule :func:`athenaeum.page_decompose._next_numeric_label` applies
    for the same reason: a dangling ref with no definition still owns its
    label, and handing it to an incoming unit would silently re-point an
    existing marker at relocated content.
    """
    used = set(parse_footnote_definitions(body))
    used |= {match.group(1) for match in INLINE_MARKER_RE.finditer(body)}
    return used


def _free_label(used: "set[str]", preferred: str) -> str:
    """*preferred* if it is free on a destination, else ``<preferred>-2``,
    ``-3``, ... (AC1's "renumbered on collision").

    Suffixing rather than renumbering to the next free INTEGER (which is
    what :func:`athenaeum.page_decompose._next_numeric_label` does for a
    single bullet) keeps the relocated marker legible against its source:
    ``src-2`` arriving on a page that already has its own ``src-2`` becomes
    ``src-2-2``, so a reader of the destination can still see which label
    the definition travelled under.
    """
    if preferred not in used:
        return preferred
    suffix = 2
    while f"{preferred}-{suffix}" in used:
        suffix += 1
    return f"{preferred}-{suffix}"


def _renumber_markers(text: str, mapping: "Mapping[str, str]") -> str:
    """Rewrite every inline ``[^old]`` in *text* whose label *mapping*
    renames (AC1). A label absent from *mapping* is left exactly as it is,
    so a unit whose labels did not collide is byte-identical.

    :func:`athenaeum.page_decompose._renumber` is the model named by
    :func:`_first_colliding_label`'s own docstring; it rewrites EVERY marker
    on a bullet to one label because a bullet carries exactly one fact. A
    unit can cite several, so this is the per-label form of the same edit.
    """
    if not mapping:
        return text

    def _replace(match: "re.Match[str]") -> str:
        label = match.group(1)
        return f"[^{mapping.get(label, label)}]"

    return INLINE_MARKER_RE.sub(_replace, text)


def _append_marker(text: str, label: str) -> str:
    """Append one ``[^label]`` marker to *text*'s last non-blank character,
    preserving its trailing whitespace exactly (AC3).

    Appending INSIDE the trailing newlines rather than after them keeps the
    marker attached to the prose it annotates: a marker on its own line
    after a blank line would render as a new paragraph.
    """
    stripped = text.rstrip()
    return f"{stripped}[^{label}]{text[len(stripped) :]}"


def relocation_note(source_name: str, source_uid: str, plan_id: str) -> str:
    """The AC3 relocation footnote's definition text: the page the unit came
    from and the plan that moved it.

    Public because it is the one place the wording lives -- a test asserting
    AC3 reads it from here rather than restating a sentence that would then
    be free to drift.
    """
    return (
        f"**Relocated from:** [[{source_uid}|{source_name}]] "
        f"by page-recompile plan {plan_id or '(unrecorded)'}"
    )


def _insert_relocated(
    target_body: str, section: str, definition_lines: "Sequence[str]"
) -> str:
    """Place *section* and *definition_lines* into *target_body* (AC1).

    The same placement rule as :func:`athenaeum.page_decompose._insert_fact`,
    which the issue's Plan names: the incoming content goes immediately
    BEFORE the first footnote-definition line (so it reads as the last of
    the page's prose, never as part of the bibliography) and the incoming
    definitions go immediately after the LAST one (so the bibliography stays
    one contiguous block). A target with no footnote definitions at all gets
    both appended.

    *section* is inserted VERBATIM, under its own headings, with no wrapper
    heading added -- AC1's own words. Nothing already on *target_body* is
    rewritten.
    """
    lines = target_body.split("\n")
    block = section.rstrip("\n").split("\n") if section.strip() else []
    definitions = [line for line in definition_lines if line.strip()]
    if not block and not definitions:
        return target_body

    def_indices = [index for index, line in enumerate(lines) if FOOTNOTE_DEF_RE.match(line)]
    if not def_indices:
        while lines and not lines[-1].strip():
            lines.pop()
        if block:
            lines.extend([""] + block)
        if definitions:
            lines.extend([""] + definitions)
        lines.append("")
        return "\n".join(lines)

    first, last = def_indices[0], def_indices[-1]
    shift = 0
    if block:
        lines[first:first] = block + [""]
        shift = len(block) + 1
    if definitions:
        insert_at = last + shift + 1
        lines[insert_at:insert_at] = definitions
    return "\n".join(lines)


def access_widening_reason(
    source_meta: "Mapping[str, Any]", target_meta: "Mapping[str, Any]"
) -> str:
    """Why *target_meta*'s read scope is WIDER than *source_meta*'s, or ``""``
    (AC2 -- a hard refusal, never a warning).

    Three independent ways a destination can be wider, checked in order of
    how much they widen:

    1. the target is world-readable (``access: open``) and the source is
       not;
    2. the target grants an ``audience:`` role the source does not -- the
       fine-grained half of the athenaeum#312 read-scoping control, which a
       coarse ``access:`` comparison alone would miss entirely;
    3. the target's coarse ``access:`` level is less restrictive than the
       source's, per :func:`athenaeum.screening.more_restrictive` (the
       repo's one ordering of ``open`` < ``internal`` < ``confidential`` <
       ``personal``, reused rather than re-ranked here).

    **An absent or malformed ``access:`` on the target reads as WIDEST**,
    because ``more_restrictive`` ranks an unknown level below ``open``. That
    is the fail-closed direction for this check: a move onto a page whose
    access cannot be established is refused rather than assumed safe.
    """
    source_access = str(source_meta.get("access") or "")
    target_access = str(target_meta.get("access") or "")
    source_roles, source_public = effective_audience(dict(source_meta))
    target_roles, target_public = effective_audience(dict(target_meta))
    if target_public and not source_public:
        return (
            f"the target is world-readable (access: {target_access or '(none)'}) "
            f"and the source is not (access: {source_access or '(none)'})"
        )
    extra_roles = sorted(target_roles - source_roles)
    if extra_roles:
        return (
            "the target grants audience role(s) the source does not: "
            + ", ".join(extra_roles)
        )
    if source_access.strip().lower() != target_access.strip().lower() and (
        more_restrictive(target_access, source_access) == source_access
    ):
        return (
            f"the target's access ({target_access or '(none)'}) is wider than "
            f"the source's ({source_access or '(none)'})"
        )
    return ""


def _derive_description(summary: str) -> str:
    """A one-line ``description:`` for the recompiled parent (AC2).

    The summary's first sentence, citation markers stripped (a frontmatter
    scalar is not the place for a ``[^label]`` marker), truncated to a safe
    length. Falls back to the whole (stripped) summary when it has no
    sentence-ending punctuation at all.
    """
    sentences = split_summary_sentences(summary)
    text = sentences[0] if sentences else summary.strip()
    text = INLINE_MARKER_RE.sub("", text).strip()
    return text[:300]


def _str_field(meta: Mapping[str, Any], key: str) -> str | None:
    """*meta*'s *key* value when it is a non-empty string, else ``None``.

    Used to inherit a parent's dimension-registry "coordinate" fields onto
    a new sub-page (AC4) without passing a non-string frontmatter value
    (e.g. a stray list) straight into a :class:`WikiEntity` field typed
    ``str | None``.
    """
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def _declared_types(wiki_root: Path) -> frozenset[str]:
    """Every type name this DEPLOYMENT accepts (issue athenaeum#2548 AC5):
    the operator-editable ``wiki/_schema/types.md`` registry union the
    package's own :data:`athenaeum.schemas.KNOWN_TYPES`.

    Both halves, because the two legitimately drift in either direction --
    :func:`athenaeum.entity_schema.declared_entity_classes`'s own docstring
    records a real corpus where a live class was absent from ``types.md``.
    A standalone page is refused only when its type is in NEITHER, which is
    the same union :func:`athenaeum.schemas.validate_wiki_meta` warns
    against.
    """
    from athenaeum.entity_schema import declared_entity_classes
    from athenaeum.schemas import KNOWN_TYPES

    return frozenset(KNOWN_TYPES) | declared_entity_classes(wiki_root)


def _schema_validation_issue(
    rendered: str, declared_types: "frozenset[str]", name: str
) -> PlanIssue | None:
    """A :class:`PlanIssue` when *rendered*'s frontmatter does not validate
    against the deployment schema (issue athenaeum#2548 AC5), else ``None``.

    Runs the SAME gate :mod:`athenaeum.page_decompose` and
    :mod:`athenaeum.intake` run on a page they create
    (:func:`athenaeum.schemas.validate_wiki_meta`), with this deployment's
    declared types passed in so a legitimately-declared type does not warn.
    Only the error SHAPE is carried into the issue detail -- never
    ``str(exc)``, which embeds the offending value and would put corpus
    content into a CLI error (the rule
    :func:`athenaeum.page_decompose._validation_error_shape` exists for).
    """
    from pydantic import ValidationError

    from athenaeum.page_decompose import _validation_error_shape
    from athenaeum.schemas import validate_wiki_meta

    meta, _body = parse_frontmatter(rendered)
    if not isinstance(meta, dict):
        return PlanIssue("standalone-frontmatter-not-a-mapping", name)
    try:
        validate_wiki_meta(dict(meta), declared_types=sorted(declared_types))
    except ValidationError as exc:
        return PlanIssue("standalone-schema-invalid", f"{name}: {_validation_error_shape(exc)}")
    return None


@dataclass(frozen=True)
class _RelocatedBody:
    """The text one destination receives from a set of relocated units."""

    section: str
    definitions: tuple[str, ...]
    inserted_texts: tuple[str, ...]


def _build_relocated_body(
    *,
    unit_ids: "Sequence[str]",
    units_by_id: "Mapping[str, Any]",
    source_definitions: "Mapping[str, str]",
    destination_body: str,
    source_name: str,
    source_uid: str,
    plan_id: str,
) -> _RelocatedBody:
    """The section and footnote definitions *unit_ids* contribute to one
    destination (issue athenaeum#2548 AC1/AC3).

    Three rules, all of them AC text:

    * every unit's text is copied VERBATIM, under its own headings, with no
      wrapper heading added -- the ONLY edits are footnote renumbering and
      the AC3 relocation marker, each of which an AC explicitly permits;
    * a definition whose label is already taken on *destination_body* with
      DIFFERENT text is renumbered (:func:`_free_label`) and every marker
      citing it in the moved text is rewritten (:func:`_renumber_markers`);
      a label free on the destination, or already there with the identical
      definition, travels unchanged and is not duplicated;
    * a unit that carries NO citation gets exactly one added relocation
      footnote naming the page it came from and the plan id
      (:func:`relocation_note`). A unit that already carries one gets
      nothing added. All the uncited units landing on one destination share
      ONE definition -- the fact being recorded is identical for each of
      them -- so each unit still carries exactly one added marker.
    """
    used = _label_namespace(destination_body)
    destination_definitions = parse_footnote_definitions(destination_body)
    mapping: dict[str, str] = {}
    definitions: list[str] = []

    for unit_id in unit_ids:
        for label in units_by_id[unit_id].footnote_labels:
            if label in mapping:
                continue
            text = source_definitions.get(label, "")
            if label in used and destination_definitions.get(label) != text:
                mapping[label] = _free_label(used, label)
            else:
                mapping[label] = label
            used.add(mapping[label])
            if destination_definitions.get(mapping[label]) != text:
                definitions.append(f"[^{mapping[label]}]: {text}")

    relocation_label = ""
    inserted: list[str] = []
    for unit_id in unit_ids:
        unit = units_by_id[unit_id]
        if unit.footnote_labels:
            inserted.append(_renumber_markers(unit.text, mapping))
            continue
        if not relocation_label:
            relocation_label = _free_label(used, f"reloc-{plan_id}" if plan_id else "reloc")
            used.add(relocation_label)
            definitions.append(
                f"[^{relocation_label}]: "
                f"{relocation_note(source_name, source_uid, plan_id)}"
            )
        inserted.append(_append_marker(unit.text, relocation_label))

    return _RelocatedBody(
        section=_join_units(inserted),
        definitions=tuple(definitions),
        inserted_texts=tuple(inserted),
    )


def _attachment_issues(
    index_map: "Mapping[str, Path]",
    *,
    child_uid: str,
    parent_uid: str,
    prospective_children: "Mapping[str, Sequence[str]]",
) -> list[PlanIssue]:
    """Every reason :func:`athenaeum.page_relations.set_relation` would
    refuse this attach, checked BEFORE any page is written (issue
    athenaeum#2548 AC6).

    ``set_relation`` is the one writer and it is the one that enforces
    these invariants -- but it raises at WRITE time, and this module's whole
    discipline is that every refusal is known before the first byte lands.
    So the same five conditions are re-asked here from the same primitives
    (:func:`athenaeum.page_relations.read_relations`,
    :func:`~athenaeum.page_relations._ancestor_uids`,
    :func:`~athenaeum.page_relations.is_inactive_relation_target`), never
    from a second copy of the rules. ``set_relation`` is still the one that
    writes, and :func:`write_apply` still reports a refusal from it, so
    this is a pre-check, not a replacement.

    *prospective_children* lets the caller declare children a page is about
    to GAIN in this same apply (the recompiled parent's new sub-pages),
    which disk cannot yet show: without it an ``attach-self-under`` on a
    page that is simultaneously being split would pass here and then be
    refused by ``set_relation`` after the parent had already been written.
    """
    child_path = index_map.get(child_uid)
    if child_path is None:
        return [PlanIssue("attach-unknown-page", child_uid)]
    parent_path = index_map.get(parent_uid)
    if parent_path is None:
        return [PlanIssue("attach-unknown-page", parent_uid)]

    parent_meta = _read_meta(parent_path)
    if child_uid == parent_uid or child_uid in _ancestor_uids(index_map, parent_meta):
        return [PlanIssue("attach-would-form-a-cycle", f"{child_uid} under {parent_uid}")]
    if is_inactive_relation_target(parent_meta):
        return [PlanIssue("attach-parent-inactive", parent_uid)]

    child_relations = read_relations(_read_meta(child_path))
    if child_relations.parent is not None and child_relations.parent.uid != parent_uid:
        return [PlanIssue("attach-child-already-has-a-parent", child_uid)]
    if read_relations(parent_meta).parent is not None:
        return [PlanIssue("attach-would-nest-three-levels", parent_uid)]
    child_children = list(prospective_children.get(child_uid, [])) or [
        c.uid for c in child_relations.children
    ]
    if child_children:
        return [PlanIssue("attach-would-nest-three-levels", child_uid)]
    return []


# ---------------------------------------------------------------------------
# The compute step (AC2-AC8): everything in memory, nothing written.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewSubPage:
    """One sub-page :func:`compute_apply` has fully rendered, not yet written."""

    uid: str
    name: str
    description: str
    group_key: str
    path: Path
    text: str
    unit_ids: tuple[str, ...]
    #: Subset of `unit_ids` placed VERBATIM (disposition `child`) -- the ones
    #: a post-write check can assert appear byte-for-byte in the written
    #: body. Excludes merge-feeder unit ids, whose own text is never written
    #: verbatim (the model's merged text replaces them).
    child_unit_ids: tuple[str, ...]
    merge_group: str | None


@dataclass(frozen=True)
class MergeAccounting:
    """One merge group's originals, recorded for the ledger (AC9)."""

    group: str
    destination_uid: str
    unit_ids: tuple[str, ...]
    unit_hashes: tuple[str, ...]


@dataclass(frozen=True)
class NewStandalonePage:
    """One ``standalone:<group>`` page, fully rendered and not yet written
    (issue athenaeum#2548 AC5).

    A sibling of :class:`NewSubPage` with ONE structural difference, which
    is the whole point of the action: it carries no ``parent:`` key, no
    breadcrumb and no ``related`` row, so it has no relation to the source
    page at all.
    """

    uid: str
    name: str
    description: str
    group_key: str
    path: Path
    text: str
    unit_ids: tuple[str, ...]
    #: The exact section text written for each unit, AFTER any footnote
    #: renumbering and any AC3 relocation marker -- what a post-write check
    #: can assert appears byte-for-byte in the written body.
    inserted_texts: tuple[str, ...]


@dataclass(frozen=True)
class TargetPageEdit:
    """One EXISTING page receiving moved units (issue athenaeum#2548 AC1), or
    an alias (AC12), computed in full and not yet written."""

    uid: str
    name: str
    path: Path
    #: The target's RAW on-disk bytes before the edit -- the ledger's
    #: ``pre_blob_id`` source, and what :func:`write_apply` restores if a
    #: LATER write in the same apply fails. Raw, never a re-render, for the
    #: reason :attr:`ComputedApply.parent_pre_text` documents.
    pre_text: str
    text: str
    pre_content_hash: str
    unit_ids: tuple[str, ...]
    inserted_texts: tuple[str, ...]
    #: ``True`` when the edit pushed the target over
    #: ``librarian.page_size_threshold_chars`` and it was flagged
    #: ``needs_split`` (AC4). Never a refusal.
    needs_split_flagged: bool = False


@dataclass(frozen=True)
class Attachment:
    """One ``attach`` / ``attach-self-under`` relation to set (issue
    athenaeum#2548 AC6), through
    :func:`athenaeum.page_relations.set_relation` with ``via="attach"``."""

    child_uid: str
    parent_uid: str
    action: str
    via: str = "attach"


@dataclass(frozen=True)
class AliasMove:
    """One alias moving off the parent and onto *target_uid* (issue
    athenaeum#2548 AC12). Only ever present when the plan LISTS it."""

    alias: str
    target_uid: str


@dataclass
class ComputedApply:
    """Everything :func:`write_apply` needs to write, computed in full
    before any file on disk is touched."""

    issues: tuple[PlanIssue, ...] = ()
    parent_path: Path | None = None
    #: The parent page's pre-image as its RAW on-disk bytes, never a
    #: re-render of the parsed frontmatter (issue athenaeum#2541). The ONLY
    #: consumer is the ``pre_blob_id`` field of the AC9 ledger record, and
    #: that id is what an undo looks up with ``git cat-file blob`` -- so it
    #: has to name bytes git actually holds. A re-render does not: it
    #: normalises the page, and a page whose frontmatter is followed by no
    #: blank line (6.0% of a seeded 600-page sample of a real 26,660-page
    #: wiki) re-renders to bytes that were never written anywhere, naming a
    #: blob that does not exist and degrading an undoable apply to a queued
    #: decision item.
    parent_pre_text: str = ""
    parent_text: str = ""
    sub_pages: tuple[NewSubPage, ...] = ()
    merges: tuple[MergeAccounting, ...] = ()
    parent_unit_ids: tuple[str, ...] = ()
    #: Cross-page outputs (issue athenaeum#2548). All four are empty for a
    #: structure-only plan, and for a cross-page plan applied under the
    #: ``approve-structure-only`` verdict (which downgrades every
    #: cross-page disposition to ``parent`` and drops every page action --
    #: see :func:`compute_apply`).
    standalone_pages: tuple[NewStandalonePage, ...] = ()
    targets: tuple[TargetPageEdit, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    alias_moves: tuple[AliasMove, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


def _unit_hash(unit_id: str) -> str:
    _ordinal, _sep, digest = unit_id.partition("-")
    return digest


def compute_apply(
    *,
    plan: RecompilePlan,
    segment: SegmentResult,
    page_meta: Mapping[str, Any],
    page_body: str,
    page_text: str,
    page_path: Path,
    wiki_root: Path,
    today: str,
    config: dict[str, Any] | None,
    client: Any,
    usage: TokenUsage | None,
    index: EntityIndex | None = None,
    plan_id: str = "",
    structure_only: bool = False,
) -> ComputedApply:
    """Validate *plan* against the LIVE page a second time and build every
    output page in memory (AC2-AC8).

    **Never trusts an already-``valid`` plan blindly.** A
    :class:`~athenaeum.page_recompile_plan.RecompilePlan` is a plain
    dataclass a caller (or a malformed ledger record) can construct directly
    with ``issues=()``/``valid`` true without ever going through
    :func:`~athenaeum.page_recompile_plan.validate_plan`'s own checks. This
    function re-derives conservation from :attr:`RecompilePlan.dispositions`
    itself (AC5/AC8) rather than reading :attr:`RecompilePlan.valid` and
    moving on -- the SAME "the model's say-so is not evidence" posture
    :mod:`athenaeum.page_recompile_plan` already applies to ``duplicate``.

    *page_text* is the page's RAW bytes as read from disk, passed in rather
    than reconstructed from *page_meta* + *page_body*, and carried straight
    through to :attr:`ComputedApply.parent_pre_text` -- see that field for
    why the difference is load-bearing (issue athenaeum#2541).

    Returns a :class:`ComputedApply` with ``issues`` populated and nothing
    else meaningful when any check fails -- the caller never writes in that
    case.

    *index* resolves a ``move``/``attach`` uid to a live page and a
    ``standalone`` name to an existing one (issue athenaeum#2548). Omitted,
    every cross-page target is unresolvable, which makes a cross-page plan
    refuse rather than silently apply as if it had no targets -- the same
    safe-posture default :func:`athenaeum.page_recompile_plan.validate_plan`
    takes for its own ``known_uids``.

    *structure_only* (AC9) is the ``approve-structure-only`` verdict: every
    ``move``/``standalone`` disposition is downgraded to ``parent`` and
    every page action is dropped, so the structure-only part of a cross-page
    plan is applied and the other units are left on the parent. It is a
    DOWNGRADE, never an upgrade -- it can only ever write less than the
    plan says.
    """
    issues: list[PlanIssue] = []
    units_by_id = {unit.unit_id: unit for unit in segment.units}
    groups_by_key = {group.key: group for group in plan.groups}
    merges_by_group = {merge.group: merge for merge in plan.merges}

    if structure_only:
        # AC9: the structure-only PART of this plan. Units routed off the
        # page entirely stay where they are; every page-level action is
        # dropped. Nothing else about the plan changes, so the whole
        # structure-only machinery below runs unmodified -- this is the one
        # and only place the verdict has an effect.
        downgraded: list[tuple[str, str]] = []
        for unit_id, disposition in plan.dispositions:
            verb, _argument = split_disposition(disposition)
            if verb in ("move", "standalone"):
                downgraded.append((unit_id, "parent"))
            else:
                downgraded.append((unit_id, disposition))
        plan = dataclasses_replace(plan, dispositions=tuple(downgraded), page_actions=())

    merge_near_duplicates = resolve_page_recompile_merge_near_duplicates(config)

    # AC11: with merging off, every `merge:<g>` disposition is treated as
    # `child:<g>` instead -- the plan is "re-planned without" its merge
    # groups rather than refused outright.
    effective: list[tuple[str, str]] = []
    for unit_id, disposition in plan.dispositions:
        verb, argument = split_disposition(disposition)
        if verb == "merge" and not merge_near_duplicates:
            effective.append((unit_id, f"child:{argument}"))
        else:
            effective.append((unit_id, disposition))

    parent_unit_ids: list[str] = []
    group_order: list[str] = []
    group_items: dict[str, list[tuple[str, str]]] = {}
    duplicate_units: dict[str, str] = {}
    move_order: list[str] = []
    move_items: dict[str, list[str]] = {}
    standalone_order: list[str] = []
    standalone_items: dict[str, list[str]] = {}
    for unit_id, disposition in effective:
        verb, argument = split_disposition(disposition)
        if unit_id not in units_by_id:
            issues.append(PlanIssue("unknown-unit", unit_id))
            continue
        if verb == "parent":
            parent_unit_ids.append(unit_id)
        elif verb in ("child", "merge"):
            if argument not in group_order:
                group_order.append(argument)
            group_items.setdefault(argument, []).append((verb, unit_id))
        elif verb == "duplicate":
            duplicate_units[unit_id] = argument
        elif verb == "move":
            # Issue athenaeum#2548 AC1. Ordered by FIRST appearance, so the
            # write order over targets is page order and therefore
            # reproducible.
            if argument not in move_order:
                move_order.append(argument)
            move_items.setdefault(argument, []).append(unit_id)
        elif verb == "standalone":
            # Issue athenaeum#2548 AC5.
            if argument not in standalone_order:
                standalone_order.append(argument)
            standalone_items.setdefault(argument, []).append(unit_id)
        else:
            # Nothing else is a per-unit disposition: `attach` /
            # `attach-self-under` are PAGE actions, and any other verb
            # means something upstream is broken.
            issues.append(PlanIssue("unsupported-disposition", disposition))
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC5/AC8: conservation, bucketed so a unit fed into a merge is
    # accounted for in ITS OWN bucket rather than also being expected
    # verbatim somewhere else.
    assigned: dict[str, list[str]] = {}
    if parent_unit_ids:
        assigned["parent"] = list(parent_unit_ids)
    for group_key, items in group_items.items():
        child_ids = [u for v, u in items if v == "child"]
        merge_ids = [u for v, u in items if v == "merge"]
        if child_ids:
            assigned[f"child:{group_key}"] = child_ids
        if merge_ids:
            assigned[f"merge:{group_key}"] = merge_ids
    for unit_id in duplicate_units:
        assigned[f"duplicate:{unit_id}"] = [unit_id]
    # AC10 of issue athenaeum#2548: the conservation check covers move
    # targets and standalone pages too -- each destination is its own
    # bucket, so a unit routed to one is accounted for exactly where it
    # lands and nowhere else.
    for target_uid, moved_ids in move_items.items():
        assigned[f"move:{target_uid}"] = list(moved_ids)
    for group_key, standalone_ids in standalone_items.items():
        assigned[f"standalone:{group_key}"] = list(standalone_ids)

    conservation = check_conservation([u.unit_id for u in segment.units], assigned)
    for unit_id in conservation.missing:
        issues.append(PlanIssue("unit-not-accounted-for", unit_id))
    for unit_id in conservation.duplicated:
        issues.append(PlanIssue("unit-accounted-for-twice", unit_id))
    if issues:
        return ComputedApply(issues=tuple(issues))

    for unit_id, target_id in duplicate_units.items():
        this = units_by_id.get(unit_id)
        other = units_by_id.get(target_id)
        if other is None:
            issues.append(PlanIssue("unknown-unit", target_id))
        elif this is not None and normalise_unit_text(this.text) != normalise_unit_text(
            other.text
        ):
            issues.append(PlanIssue("duplicate-text-mismatch", unit_id))
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC7: merge-section validation.
    original_definitions = parse_footnote_definitions(page_body)
    merge_sections: dict[str, MergedSection] = {}
    for group_key, items in group_items.items():
        merge_ids = [u for v, u in items if v == "merge"]
        if not merge_ids:
            continue
        section = merges_by_group.get(group_key)
        if section is None:
            issues.append(PlanIssue("merge-group-missing", group_key))
            continue
        if tuple(sorted(section.unit_ids)) != tuple(sorted(merge_ids)):
            issues.append(PlanIssue("merge-group-mismatch", group_key))
            continue
        feeder_units = [units_by_id[u] for u in merge_ids]
        required_labels = {label for u in feeder_units for label in u.footnote_labels}
        merged_labels = {m.group(1) for m in INLINE_MARKER_RE.finditer(section.text)}
        for label in sorted(required_labels - merged_labels):
            issues.append(PlanIssue("merge-dropped-citation", label))
        for label in sorted(required_labels):
            if label not in original_definitions:
                issues.append(PlanIssue("merge-dangling-citation", label))
        total_len = sum(len(u.text) for u in feeder_units)
        if len(section.text) > total_len:
            issues.append(PlanIssue("merge-too-long", group_key))
        merge_sections[group_key] = section
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC7(d): a verify call, driven through the SAME `verify_summary` seam
    # the planner uses -- the merged text stands in for a "summary" and its
    # feeder units stand in for the "segment" being summarised, so the exact
    # citation-support check already built for AC9 of athenaeum#2537 applies
    # unchanged rather than being re-implemented here.
    for group_key, section in merge_sections.items():
        merge_ids = [u for v, u in group_items[group_key] if v == "merge"]
        feeder_units_tuple = tuple(units_by_id[u] for u in merge_ids)
        if client is None:
            issues.append(PlanIssue("merge-verify-unavailable", group_key))
            continue
        fake_plan = RecompilePlan(
            uid=plan.uid,
            content_hash=plan.content_hash,
            dispositions=(),
            groups=(),
            merges=(),
            page_actions=(),
            summary=section.text,
            scope=plan.scope,
            issues=(),
            verified=None,
        )
        fake_segment = SegmentResult(units=feeder_units_tuple, footnote_block="")
        verified_plan = verify_summary(
            fake_plan, fake_segment, client, config=config, usage=usage
        )
        if verified_plan.verified is not True:
            issues.append(PlanIssue("merge-unverified", group_key))
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC3: the parent must keep a configurable minimum of cited prose.
    summary_block = plan.summary.strip()
    parent_units_text = _join_units(units_by_id[u].text for u in parent_unit_ids)
    prose_chars = len(summary_block) + len(parent_units_text.strip())
    min_parent_chars = resolve_page_recompile_min_parent_chars(config)
    if prose_chars < min_parent_chars:
        issues.append(PlanIssue("parent-too-thin", str(prose_chars)))
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC3 (nesting invariant, mirrors page_relations.set_relation's own
    # third-level refusal): a page that is ITSELF a child cannot also
    # become a parent.
    existing_relations = read_relations(page_meta)
    if existing_relations.parent is not None:
        issues.append(PlanIssue("page-already-has-parent", existing_relations.parent.uid))
        return ComputedApply(issues=tuple(issues))

    parent_uid = str(page_meta.get("uid") or plan.uid)
    parent_name = str(page_meta.get("name") or parent_uid)
    parent_type = str(page_meta.get("type") or "")
    parent_access = str(page_meta.get("access") or "internal")
    raw_tags = page_meta.get("tags")
    parent_tags = list(raw_tags) if isinstance(raw_tags, list) else []

    # AC4: new sub-pages, named "<Parent name> (<Sub-topic>)", inheriting
    # access/tags/coordinates -- never wider access than the parent, which
    # holds by construction since access is copied, not derived.
    sub_pages: list[NewSubPage] = []
    merge_accounting: list[MergeAccounting] = []
    for group_key in group_order:
        items = group_items[group_key]
        section = merge_sections.get(group_key)
        group: PlanGroup | None = groups_by_key.get(group_key)
        texts: list[str] = []
        unit_ids: list[str] = []
        child_unit_ids: list[str] = []
        merge_inserted = False
        for verb, unit_id in items:
            if verb == "child":
                texts.append(renormalise_heading(units_by_id[unit_id].text))
                unit_ids.append(unit_id)
                child_unit_ids.append(unit_id)
            elif verb == "merge":
                if not merge_inserted and section is not None:
                    texts.append(section.text)
                    merge_inserted = True
                unit_ids.append(unit_id)
        body_core = _join_units(texts)

        label_pairs: list[tuple[str, str]] = [
            (label, original_definitions.get(label, ""))
            for unit_id in unit_ids
            for label in units_by_id[unit_id].footnote_labels
        ]
        if section is not None:
            label_pairs.extend(
                (m.group(1), original_definitions.get(m.group(1), ""))
                for m in INLINE_MARKER_RE.finditer(section.text)
            )
        collision = _first_colliding_label(label_pairs)
        if collision is not None:
            sub_name_for_issue = (group.name if group is not None else group_key)
            issues.append(
                PlanIssue("footnote-label-collision", f"{collision} on {sub_name_for_issue}")
            )
            continue
        label_set = {label for label, _ in label_pairs}
        footnotes = _render_footnote_block(label_set, original_definitions)
        body_with_footnotes = _join_units([body_core, footnotes])

        sub_uid = generate_uid()
        sub_name_part = group.name if group is not None else group_key
        sub_name = f"{parent_name} ({sub_name_part})"
        description = group.description if group is not None else ""
        body_with_breadcrumb = apply_breadcrumb(body_with_footnotes, parent_uid, parent_name)

        entity = WikiEntity(
            uid=sub_uid,
            type=parent_type,
            name=sub_name,
            access=parent_access,
            tags=list(parent_tags),
            related=[],
            created=today,
            updated=today,
            body=body_with_breadcrumb,
            source=f"page-recompile-apply:{parent_uid}:{today}",
            # AC4: inherit the dimension-registry "coordinates" too --
            # provenance_scope/claimed_scope/subject -- same posture as
            # access/tags above.
            provenance_scope=_str_field(page_meta, "provenance_scope"),
            claimed_scope=_str_field(page_meta, "claimed_scope"),
            subject=_str_field(page_meta, "subject"),
        )
        # AC4: the child's `parent:` link (page_relations.py's new-shape
        # relation, via="split") -- WikiEntity itself carries no `parent`
        # field, so it is spliced into the rendered frontmatter the same
        # way every other post-render frontmatter edit in this module works.
        rendered = entity.render()
        sub_meta, sub_body = parse_frontmatter(rendered)
        sub_meta["parent"] = parent_uid
        sub_text = render_frontmatter(sub_meta) + "\n" + sub_body

        sub_pages.append(
            NewSubPage(
                uid=sub_uid,
                name=sub_name,
                description=description,
                group_key=group_key,
                path=wiki_root / entity.filename,
                text=sub_text,
                unit_ids=tuple(unit_ids),
                child_unit_ids=tuple(child_unit_ids),
                merge_group=group_key if section is not None else None,
            )
        )
        if section is not None:
            merge_unit_ids = tuple(u for v, u in items if v == "merge")
            merge_accounting.append(
                MergeAccounting(
                    group=group_key,
                    destination_uid=sub_uid,
                    unit_ids=merge_unit_ids,
                    unit_hashes=tuple(_unit_hash(u) for u in merge_unit_ids),
                )
            )
    if issues:
        return ComputedApply(issues=tuple(issues))

    # -----------------------------------------------------------------
    # Issue athenaeum#2548: standalone pages (AC5), move targets
    # (AC1-AC4), page actions (AC6/AC7) and alias moves (AC12).
    # -----------------------------------------------------------------
    threshold_chars = resolve_page_size_threshold_chars(config)
    standalone_pages: list[NewStandalonePage] = []
    if standalone_order:
        declared_types = _declared_types(wiki_root)
        for group_key in standalone_order:
            group = groups_by_key.get(group_key)
            name = (group.name if group is not None else "").strip()
            if not name:
                issues.append(PlanIssue("standalone-missing-name", group_key))
                continue
            # AC5: a name that already resolves is a MOVE, and the planner
            # (`page_recompile_plan.resolve_standalone_groups`) converts it
            # before the plan is recorded. Reaching here means the plan an
            # operator APPROVED said "create a new page" over a name that
            # now resolves -- refused, never silently converted, because
            # converting would apply something they did not approve.
            if index is not None and index.lookup(name) is not None:
                issues.append(PlanIssue("standalone-name-already-exists", name))
                continue
            if parent_type and parent_type not in declared_types:
                issues.append(PlanIssue("standalone-type-not-declared", parent_type))
                continue
            built = _build_relocated_body(
                unit_ids=standalone_items[group_key],
                units_by_id=units_by_id,
                source_definitions=original_definitions,
                destination_body="",
                source_name=parent_name,
                source_uid=parent_uid,
                plan_id=plan_id,
            )
            entity = WikiEntity(
                uid=generate_uid(),
                type=parent_type,
                name=name,
                access=parent_access,
                tags=list(parent_tags),
                related=[],
                created=today,
                updated=today,
                # AC5: no relation to the source page -- no `parent:` key,
                # no breadcrumb, no `related` row. That absence IS the
                # action: a standalone page is not a child.
                # `lstrip` because `_insert_relocated`'s empty-destination
                # branch separates the incoming section from what was there
                # before -- and on a brand-new page there was nothing, so
                # the separator would render as a blank first line.
                body=_insert_relocated("", built.section, built.definitions).lstrip("\n"),
                source=f"page-recompile-standalone:{parent_uid}:{today}",
                provenance_scope=_str_field(page_meta, "provenance_scope"),
                claimed_scope=_str_field(page_meta, "claimed_scope"),
                subject=_str_field(page_meta, "subject"),
            )
            rendered = entity.render()
            schema_issue = _schema_validation_issue(rendered, declared_types, name)
            if schema_issue is not None:
                issues.append(schema_issue)
                continue
            standalone_pages.append(
                NewStandalonePage(
                    uid=entity.uid,
                    name=name,
                    description=group.description if group is not None else "",
                    group_key=group_key,
                    path=wiki_root / entity.filename,
                    text=rendered,
                    unit_ids=tuple(standalone_items[group_key]),
                    inserted_texts=built.inserted_texts,
                )
            )

    target_edits: dict[str, TargetPageEdit] = {}
    for target_uid in move_order:
        target_path = index.get_by_uid(target_uid) if index is not None else None
        if target_path is None:
            issues.append(PlanIssue("move-target-missing", target_uid))
            continue
        if target_uid == parent_uid:
            issues.append(PlanIssue("move-target-is-the-source-page", target_uid))
            continue
        try:
            target_text = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            issues.append(PlanIssue("move-target-unreadable", target_uid))
            continue
        target_meta, target_body = parse_frontmatter(target_text)
        if not isinstance(target_meta, dict):
            issues.append(PlanIssue("move-target-frontmatter-not-a-mapping", target_uid))
            continue
        if is_inactive_relation_target(target_meta):
            issues.append(PlanIssue("move-target-inactive", target_uid))
            continue
        # AC2: a HARD refusal, with no write, never a warning.
        widening = access_widening_reason(page_meta, target_meta)
        if widening:
            issues.append(
                PlanIssue("move-target-access-wider", f"{target_uid}: {widening}")
            )
            continue
        built = _build_relocated_body(
            unit_ids=move_items[target_uid],
            units_by_id=units_by_id,
            source_definitions=original_definitions,
            destination_body=target_body,
            source_name=parent_name,
            source_uid=parent_uid,
            plan_id=plan_id,
        )
        new_target_body = _insert_relocated(target_body, built.section, built.definitions)
        # AC1: the target's frontmatter IDENTITY is unchanged -- `updated`
        # is bookkeeping (and the same bump `page_decompose._bump_and_render`
        # makes when it attaches a fact to a target), `needs_split` is AC4.
        new_target_meta: dict[str, Any] = dict(target_meta)
        new_target_meta["updated"] = today
        over_threshold = len(new_target_body) > threshold_chars
        if over_threshold:
            # AC4: flagged, and therefore a candidate on the next scan. The
            # move is NOT refused for size.
            new_target_meta["needs_split"] = True
        target_edits[target_uid] = TargetPageEdit(
            uid=target_uid,
            name=str(target_meta.get("name") or target_uid),
            path=target_path,
            pre_text=target_text,
            text=render_frontmatter(new_target_meta) + "\n" + new_target_body,
            pre_content_hash=page_content_hash(target_body),
            unit_ids=tuple(move_items[target_uid]),
            inserted_texts=built.inserted_texts,
            needs_split_flagged=over_threshold,
        )
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC6/AC7/AC12: page-level actions.
    index_map = _index_pages_by_uid(wiki_root)
    prospective_children = {
        parent_uid: [c.uid for c in existing_relations.children] + [sp.uid for sp in sub_pages]
    }
    attachments: list[Attachment] = []
    alias_moves: list[AliasMove] = []
    raw_parent_aliases = page_meta.get("aliases")
    parent_aliases = (
        [a for a in raw_parent_aliases if isinstance(a, str)]
        if isinstance(raw_parent_aliases, list)
        else []
    )
    for action in plan.page_actions:
        pair = parse_alias_move(action)
        if pair is not None:
            alias_target_uid, alias_text = pair
            if alias_text not in parent_aliases:
                issues.append(PlanIssue("alias-move-alias-not-on-the-page", alias_text))
                continue
            if alias_target_uid not in index_map:
                issues.append(PlanIssue("alias-move-target-missing", alias_target_uid))
                continue
            alias_target_meta = _read_meta(index_map[alias_target_uid])
            if is_inactive_relation_target(alias_target_meta):
                issues.append(PlanIssue("alias-move-target-inactive", alias_target_uid))
                continue
            alias_widening = access_widening_reason(page_meta, alias_target_meta)
            if alias_widening:
                issues.append(
                    PlanIssue(
                        "alias-move-target-access-wider",
                        f"{alias_target_uid}: {alias_widening}",
                    )
                )
                continue
            alias_moves.append(AliasMove(alias=alias_text, target_uid=alias_target_uid))
            continue
        verb, argument = split_disposition(action)
        if verb not in PAGE_ACTIONS or not argument:
            issues.append(PlanIssue("unknown-page-action", action))
            continue
        if verb == "attach":
            child_uid, attach_parent_uid = argument, parent_uid
        else:  # attach-self-under
            child_uid, attach_parent_uid = parent_uid, argument
        attach_issues = _attachment_issues(
            index_map,
            child_uid=child_uid,
            parent_uid=attach_parent_uid,
            prospective_children=prospective_children,
        )
        if attach_issues:
            issues.extend(attach_issues)
            continue
        attachments.append(
            Attachment(child_uid=child_uid, parent_uid=attach_parent_uid, action=action)
        )
    if issues:
        return ComputedApply(issues=tuple(issues))

    # AC12: an alias leaves the parent ONLY when the plan listed it, and it
    # lands on the named target in the same apply.
    moved_aliases = {move.alias for move in alias_moves}
    for move in alias_moves:
        edit = target_edits.get(move.target_uid)
        if edit is None:
            alias_path = index_map[move.target_uid]
            alias_meta_raw, alias_body = parse_frontmatter(
                alias_path.read_text(encoding="utf-8")
            )
            alias_meta = dict(alias_meta_raw) if isinstance(alias_meta_raw, dict) else {}
            edit = TargetPageEdit(
                uid=move.target_uid,
                name=str(alias_meta.get("name") or move.target_uid),
                path=alias_path,
                pre_text=alias_path.read_text(encoding="utf-8"),
                text="",
                pre_content_hash=page_content_hash(alias_body),
                unit_ids=(),
                inserted_texts=(),
            )
            base_meta, base_body = alias_meta, alias_body
        else:
            base_meta_raw, base_body = parse_frontmatter(edit.text)
            base_meta = dict(base_meta_raw) if isinstance(base_meta_raw, dict) else {}
        raw_existing = base_meta.get("aliases")
        existing = (
            [a for a in raw_existing if isinstance(a, str)]
            if isinstance(raw_existing, list)
            else []
        )
        if move.alias not in existing:
            existing.append(move.alias)
        base_meta["aliases"] = existing
        base_meta["updated"] = today
        target_edits[move.target_uid] = dataclasses_replace(
            edit, text=render_frontmatter(base_meta) + "\n" + base_body
        )

    # AC4: new children linked via the page_relations.py writer's shape
    # (`via="split"`), folded together with any pre-existing children in
    # ONE write rather than one `set_relation` call per child -- a repeated
    # double-write per child would rewrite the parent N times with
    # intermediate (not-yet-final) content, breaking the "parent written
    # once, last, with its final body" contract this module (and the
    # retired reactive split, tiers.py:_perform_oversize_page_split,
    # before it) relies on.
    new_children = [ChildRef(uid=sp.uid, via="split") for sp in sub_pages]
    all_children = list(existing_relations.children) + new_children
    all_children.sort(key=lambda c: c.uid)
    other_rows = [row for row in _related_rows(page_meta) if not _is_child_relation_row(row)]
    new_related = other_rows + [
        {"uid": c.uid, "role": ROLE_CHILD, "via": c.via} for c in all_children
    ]

    new_by_uid = {sp.uid: sp for sp in sub_pages}
    if existing_relations.children:
        index_map = _index_pages_by_uid(wiki_root)
        existing_rows = {
            uid: (name, desc)
            for uid, name, desc in _children_display_rows(
                index_map, list(existing_relations.children)
            )
        }
    else:
        existing_rows = {}
    display_rows: list[tuple[str, str, str]] = []
    for child in all_children:
        if child.uid in new_by_uid:
            sp = new_by_uid[child.uid]
            display_rows.append((sp.uid, sp.name, sp.description))
        else:
            name, desc = existing_rows.get(child.uid, (child.uid, ""))
            display_rows.append((child.uid, name, desc))

    # AC2/AC3: parent body = cited summary lead, then `parent` units
    # verbatim, then the Sub-pages section.
    parent_label_pairs = [
        (label, original_definitions.get(label, ""))
        for u in parent_unit_ids
        for label in units_by_id[u].footnote_labels
    ]
    parent_collision = _first_colliding_label(parent_label_pairs)
    if parent_collision is not None:
        return ComputedApply(
            issues=(PlanIssue("footnote-label-collision", f"{parent_collision} on parent"),)
        )
    parent_footnote_labels = {label for label, _ in parent_label_pairs}
    parent_footnotes = _render_footnote_block(parent_footnote_labels, original_definitions)
    parent_core = _join_units(
        [summary_block + ("\n" if summary_block else ""), parent_units_text, parent_footnotes]
    )
    new_parent_body = apply_sub_pages_section(parent_core, display_rows)

    # AC2: frontmatter byte-identical outside exactly these four keys --
    # plus `aliases`, and then ONLY when the plan listed an alias move
    # (issue athenaeum#2548 AC12).
    new_meta: dict[str, Any] = dict(page_meta)
    new_meta["updated"] = today
    new_meta["description"] = _derive_description(plan.summary)
    if new_related:
        new_meta["related"] = new_related
    else:
        new_meta.pop("related", None)
    # Issue athenaeum#2548 AC7: `needs_split` is re-evaluated only when
    # content actually LEFT the parent. A plan whose only action is
    # `attach-self-under` (every unit stays, no group, no move, no
    # standalone) routes nothing away, so the flag is left exactly as it
    # was -- clearing it would assert a page had been split when it had
    # not, and that flag is what puts the page back on the candidate scan.
    units_left_the_parent = bool(group_order or move_order or standalone_order)
    if units_left_the_parent and len(new_parent_body) <= threshold_chars:
        new_meta.pop("needs_split", None)
    if moved_aliases:
        remaining_aliases = [a for a in parent_aliases if a not in moved_aliases]
        if remaining_aliases:
            new_meta["aliases"] = remaining_aliases
        else:
            new_meta.pop("aliases", None)
    new_parent_text = render_frontmatter(new_meta) + "\n" + new_parent_body

    return ComputedApply(
        issues=(),
        parent_path=page_path,
        # RAW bytes, not `render_frontmatter(page_meta) + "\n" + page_body`
        # (issue athenaeum#2541) -- see ComputedApply.parent_pre_text.
        parent_pre_text=page_text,
        parent_text=new_parent_text,
        sub_pages=tuple(sub_pages),
        merges=tuple(merge_accounting),
        parent_unit_ids=tuple(parent_unit_ids),
        standalone_pages=tuple(standalone_pages),
        targets=tuple(target_edits[uid] for uid in sorted(target_edits)),
        attachments=tuple(attachments),
        alias_moves=tuple(alias_moves),
    )


# ---------------------------------------------------------------------------
# Resolving which plan to apply (AC1), and the orchestrator.
# ---------------------------------------------------------------------------


def _plan_from_record(record: Mapping[str, Any]) -> RecompilePlan:
    """Reconstruct a :class:`RecompilePlan` from its ``to_dict()`` shape
    (the ledger's own on-disk record), the inverse of
    :meth:`RecompilePlan.to_dict`."""
    raw_dispositions = record.get("dispositions") or []
    dispositions = tuple(
        (str(d.get("unit") or ""), str(d.get("disposition") or ""))
        for d in raw_dispositions
        if isinstance(d, dict)
    )
    raw_groups = record.get("groups") or []
    groups = tuple(
        PlanGroup(
            key=str(g.get("key") or ""),
            name=str(g.get("name") or ""),
            description=str(g.get("description") or ""),
            confidence=float(g.get("confidence") or 0.0),
        )
        for g in raw_groups
        if isinstance(g, dict)
    )
    raw_merges = record.get("merges") or []
    merges = tuple(
        MergedSection(
            group=str(m.get("group") or ""),
            text=str(m.get("text") or ""),
            unit_ids=tuple(m.get("units") or ()),
        )
        for m in raw_merges
        if isinstance(m, dict)
    )
    raw_issues = record.get("issues") or []
    issues = tuple(
        PlanIssue(code=str(i.get("code") or ""), detail=str(i.get("detail") or ""))
        for i in raw_issues
        if isinstance(i, dict)
    )
    return RecompilePlan(
        uid=str(record.get("uid") or ""),
        content_hash=str(record.get("content_hash") or ""),
        dispositions=dispositions,
        groups=groups,
        merges=merges,
        page_actions=tuple(record.get("page_actions") or ()),
        summary=str(record.get("summary") or ""),
        scope=str(record.get("scope") or ""),
        issues=issues,
        verified=record.get("verified"),
        verify_attempted=bool(record.get("verify_attempted") or False),
    )


def _load_plan_for_apply(
    wiki_root: Path, *, uid: str, plan_id: str, ledger_path: Path | None
) -> tuple[RecompilePlan | None, str, str]:
    """``(plan, resolved_plan_id, error)`` -- the plan AC1's ``--uid``/``--plan``
    names, or ``(None, "", error)``.

    ``--plan <id>`` reads that exact record. ``--uid U`` reads the LAST
    ``valid`` plan record for that uid (the ledger is append-only, so a
    re-planned page's newer record is the current one -- the same rule
    :func:`~athenaeum.page_recompile_plan.find_plan_record` already applies
    per plan id, applied here per uid instead).
    """
    if plan_id:
        record = find_plan_record(plan_id, wiki_root, ledger_path=ledger_path)
        if record is None:
            return None, plan_id, f"no plan with id {plan_id}"
        plan_dict = record.get("plan")
        if not isinstance(plan_dict, dict):
            return None, plan_id, f"plan record {plan_id} is malformed"
        return _plan_from_record(plan_dict), plan_id, ""

    if not uid:
        return None, "", "either --uid or --plan is required"

    match: dict[str, Any] | None = None
    match_id = ""
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") != "plan":
            continue
        plan_dict = record.get("plan")
        if not isinstance(plan_dict, dict) or plan_dict.get("uid") != uid:
            continue
        if not plan_dict.get("valid"):
            continue
        match = plan_dict
        match_id = str(record.get("id") or "")
    if match is None:
        return None, "", f"no valid plan recorded for uid {uid}"
    return _plan_from_record(match), match_id, ""


@dataclass
class ApplyOutcome:
    """What :func:`write_apply` produced, for the CLI to report."""

    ok: bool
    reason: str
    issues: tuple[PlanIssue, ...] = ()
    plan_id: str = ""
    uid: str = ""
    created_uids: tuple[str, ...] = ()
    snapshot_sha: str | None = None
    ledger_record_id: str = ""
    post_write_issues: tuple[PlanIssue, ...] = field(default_factory=tuple)


def _build_ledger_record(
    *,
    plan_id: str,
    uid: str,
    computed: ComputedApply,
    pre_parent_hash: str,
    post_parent_hash: str,
    target_post_hashes: "Mapping[str, str] | None" = None,
    attach_pages: "Sequence[Mapping[str, Any]]" = (),
    attachments: "Sequence[Attachment] | None" = None,
) -> dict[str, Any]:
    """The AC9 record: pre/post content hash and the pre-image git blob id
    for every touched page, the created uids, which units landed where, and
    the merged groups' original unit ids/hashes.

    Issue athenaeum#2548 AC10 widens "every touched page" to the move
    targets, the created standalone pages, and the two pages an ``attach``
    writes -- each with the same four fields, so
    :mod:`athenaeum.page_recompile_undo` needs no new reader to reverse
    them. *target_post_hashes* and *attach_pages* are measured AFTER the
    write (and, for the attach pair, after
    :func:`athenaeum.page_relations.set_relation`), because that is the only
    moment they are true.
    """
    pages: list[dict[str, Any]] = [
        {
            "uid": uid,
            "path": str(computed.parent_path),
            "pre_content_hash": pre_parent_hash,
            "post_content_hash": post_parent_hash,
            "pre_blob_id": _git_blob_id(computed.parent_pre_text.encode("utf-8")),
            "units": list(computed.parent_unit_ids),
        }
    ]
    for sp in computed.sub_pages:
        pages.append(
            {
                "uid": sp.uid,
                "path": str(sp.path),
                "pre_content_hash": "",
                "post_content_hash": page_content_hash(parse_frontmatter(sp.text)[1]),
                "pre_blob_id": "",
                "units": list(sp.unit_ids),
            }
        )
    for standalone in computed.standalone_pages:
        pages.append(
            {
                "uid": standalone.uid,
                "path": str(standalone.path),
                "pre_content_hash": "",
                "post_content_hash": page_content_hash(
                    parse_frontmatter(standalone.text)[1]
                ),
                "pre_blob_id": "",
                "units": list(standalone.unit_ids),
            }
        )
    for target in computed.targets:
        pages.append(
            {
                "uid": target.uid,
                "path": str(target.path),
                "pre_content_hash": target.pre_content_hash,
                "post_content_hash": (target_post_hashes or {}).get(target.uid, ""),
                "pre_blob_id": _git_blob_id(target.pre_text.encode("utf-8")),
                "units": list(target.unit_ids),
            }
        )
    recorded_uids = {str(row.get("uid") or "") for row in pages}
    for row in attach_pages:
        if str(row.get("uid") or "") in recorded_uids:
            continue
        pages.append(dict(row))
    return {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": APPLY_KIND,
        "id": plan_id,
        "ts": now_iso(),
        "uid": uid,
        "created_uids": [sp.uid for sp in computed.sub_pages]
        + [sp.uid for sp in computed.standalone_pages],
        "pages": pages,
        "merges": [
            {
                "group": m.group,
                "destination_uid": m.destination_uid,
                "unit_ids": list(m.unit_ids),
                "unit_hashes": list(m.unit_hashes),
            }
            for m in computed.merges
        ],
        # Issue athenaeum#2548 AC11: what an undo has to reverse that a
        # pre-image restore alone does not NAME -- the relations an attach
        # set (undone by a detach) and the aliases a plan moved.
        "moves": [
            {"uid": t.uid, "units": list(t.unit_ids)}
            for t in computed.targets
            if t.unit_ids
        ],
        "attachments": [
            {"child": a.child_uid, "parent": a.parent_uid, "via": a.via, "action": a.action}
            for a in (computed.attachments if attachments is None else attachments)
        ],
        "alias_moves": [
            {"alias": a.alias, "target": a.target_uid} for a in computed.alias_moves
        ],
    }


#: The two verdicts that authorise a write (issue athenaeum#2548 AC8),
#: read off :mod:`athenaeum.page_recompile_queue`'s own vocabulary rather
#: than restated as literals here -- the queue module owns the verdict
#: words, and three string literals agreeing by eye is how they drift.
APPROVING_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_APPROVE, VERDICT_APPROVE_STRUCTURE_ONLY}
)


def _cross_page_approval_refusal(
    wiki_root: Path,
    *,
    plan_id: str,
    verdict: str,
    ledger_path: Path | None,
) -> str:
    """Why a CROSS-PAGE plan may not be applied, or ``""`` (issue
    athenaeum#2548 AC8: "never applied without an approved decision item,
    in every mode").

    Two independent halves, because either alone is forgeable:

    * the caller must present an approving *verdict*. Only
      :func:`athenaeum.decision_answers._apply_page_recompile_answer` has
      one, which is what keeps ``athenaeum recompile apply`` -- an operator
      naming a uid directly -- from applying a cross-page plan behind the
      decision queue's back.
    * the ledger must hold a :data:`~athenaeum.page_recompile_queue.QUEUE_KIND`
      record for this plan id, so the plan really was RAISED as an item.
      Without this a caller passing ``verdict="approve"`` could apply a
      plan nobody was ever asked about.

    **The run MODE is deliberately not consulted.** ``mode`` decides
    whether the nightly phase drafts and raises a plan at all; it has no
    say over whether an answered plan may be written. Reading it here would
    make the refusal mode-dependent, and AC8 requires it to hold in every
    mode -- ``auto`` included.
    """
    if verdict.strip().lower() not in APPROVING_VERDICTS:
        return (
            "cross-page plan: refused, because a plan that moves units to "
            "another page, creates a standalone page, attaches a page or "
            "moves an alias is only ever applied through an approved "
            f"page-recompile decision item (verdict given: {verdict!r})"
        )
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") == QUEUE_KIND and str(record.get("id") or "") == plan_id:
            return ""
    return (
        "cross-page plan: refused, because no page-recompile decision item "
        f"was ever raised for plan {plan_id}"
    )


def _archive_oversize_notices_for_applied_plan(
    wiki_root: Path, *, name: str, plan_id: str
) -> int:
    """Archive any open oversize-family notice for *name* once a recompile
    plan has applied to its page (issue athenaeum#2550 AC6).

    An unanswered :data:`~athenaeum.tiers.OVERSIZE_ESCALATION_CONFLICT_TYPES`
    block (``oversize_page``, ``oversize_split`` or ``oversize_log_demote``)
    is a record that a page needed restructuring; a plan just landing on
    that same page is exactly the restructuring it was asking for, so the
    notice is stale. Archived through
    :func:`athenaeum.tiers.archive_pending_blocks` with a reason naming
    *plan_id*, never silently deleted.

    Matched by the page's ``name:`` (the same free-text key
    :func:`~athenaeum.tiers.tier4_escalate`'s own oversize-family dedup
    already keys on -- there is no uid field on a ``PendingQuestion``).

    A notice still carrying a parked observation
    (``pq.recovered_observation``) is left OPEN, same carve-out
    :func:`~athenaeum.tiers.close_moot_notices` already applies: the
    observation it is protecting was never merged anywhere, so closing the
    notice would be the only place that text was held.

    Returns the number of blocks archived (``0`` when there is no pending
    file, no *name*, or no matching unanswered notice without a parked
    observation).
    """
    if not name:
        return 0
    pending_path = wiki_root / "_pending_questions.md"
    if not pending_path.exists():
        return 0
    from athenaeum.answers import parse_pending_questions
    from athenaeum.tiers import OVERSIZE_ESCALATION_CONFLICT_TYPES, archive_pending_blocks

    questions = parse_pending_questions(pending_path)
    to_archive = {
        pq.raw_block
        for pq in questions
        if not pq.answered
        and pq.conflict_type in OVERSIZE_ESCALATION_CONFLICT_TYPES
        and pq.entity == name
        and not pq.recovered_observation
    }
    if not to_archive:
        return 0
    return archive_pending_blocks(
        pending_path,
        questions,
        to_archive,
        reason=f"page recompile plan {plan_id} applied to this page (issue athenaeum#2550)",
    )


def write_apply(
    wiki_root: Path,
    *,
    uid: str = "",
    plan_id: str = "",
    client: Any = None,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    now: date | None = None,
    verdict: str = "",
) -> ApplyOutcome:
    """Apply ONE recompile plan end to end (issues athenaeum#2540, athenaeum#2548).

    Rolls back a stale journal first (AC10), resolves and re-validates the
    plan (AC1, :func:`compute_apply`), writes every NEW page, then every
    existing move target, then the parent LAST under journal protection,
    sets any attach relation, re-verifies the written pages, appends the
    AC9 ledger record, and makes one snapshot commit
    (:meth:`athenaeum.store.FilesystemStore.snapshot`).

    *verdict* is the operator's answer, supplied by
    :func:`athenaeum.decision_answers._apply_page_recompile_answer` (issue
    athenaeum#2548). A **structure-only** plan ignores it: it needs no
    approval beyond the existing run lock, exactly as before. A
    **cross-page** plan is refused outright unless *verdict* is an
    approving one AND this plan was genuinely raised as a
    ``page-recompile`` decision item -- see
    :func:`_cross_page_approval_refusal` for why both halves are checked
    here rather than trusted from the caller.

    ``approve-structure-only`` then applies only the structure-only PART of
    such a plan (AC9). That is DERIVED from *verdict* here rather than
    taken as a second parameter on purpose: two arguments that have to
    agree are two arguments that can disagree, and the failure mode of
    disagreeing is applying a cross-page action under a verdict that
    authorised only the in-page restructure.
    """
    rollback_stale_journal(wiki_root)

    plan, resolved_plan_id, error = _load_plan_for_apply(
        wiki_root, uid=uid, plan_id=plan_id, ledger_path=ledger_path
    )
    if plan is None:
        return ApplyOutcome(ok=False, reason=error, plan_id=resolved_plan_id, uid=uid)

    if plan.scope != SCOPE_STRUCTURE_ONLY:
        refusal = _cross_page_approval_refusal(
            wiki_root,
            plan_id=resolved_plan_id,
            verdict=verdict,
            ledger_path=ledger_path,
        )
        if refusal:
            return ApplyOutcome(
                ok=False,
                reason=refusal,
                plan_id=resolved_plan_id,
                uid=plan.uid,
            )

    index = EntityIndex(wiki_root)
    page_path = index.get_by_uid(plan.uid)
    if page_path is None:
        return ApplyOutcome(
            ok=False, reason=f"no page with uid {plan.uid}", plan_id=resolved_plan_id, uid=plan.uid
        )
    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ApplyOutcome(
            ok=False, reason=f"unreadable page: {exc}", plan_id=resolved_plan_id, uid=plan.uid
        )
    page_meta, page_body = parse_frontmatter(text)
    if not isinstance(page_meta, dict):
        return ApplyOutcome(
            ok=False,
            reason="page frontmatter did not parse to a mapping",
            plan_id=resolved_plan_id,
            uid=plan.uid,
        )

    # AC1: a plan whose pinned content hash no longer matches is stale.
    if page_content_hash(page_body) != plan.content_hash:
        return ApplyOutcome(
            ok=False,
            reason="stale: the page's content has changed since this plan was made",
            plan_id=resolved_plan_id,
            uid=plan.uid,
        )

    segment = segment_page(page_body)
    today = (now or date.today()).isoformat()
    usage = TokenUsage()
    computed = compute_apply(
        plan=plan,
        segment=segment,
        page_meta=page_meta,
        page_body=page_body,
        page_text=text,
        page_path=page_path,
        wiki_root=wiki_root,
        today=today,
        config=config,
        client=client,
        usage=usage,
        index=index,
        plan_id=resolved_plan_id,
        structure_only=verdict.strip().lower() == VERDICT_APPROVE_STRUCTURE_ONLY,
    )
    if not computed.ok:
        return ApplyOutcome(
            ok=False,
            reason="plan rejected at apply time",
            issues=computed.issues,
            plan_id=resolved_plan_id,
            uid=plan.uid,
        )

    assert computed.parent_path is not None  # ok implies this is set

    # The Plan's write order, in one place: NEW pages first (sub-pages then
    # standalone pages -- a pure addition either way), then every existing
    # move target, then the parent LAST.
    new_paths = [sp.path for sp in computed.sub_pages] + [
        sp.path for sp in computed.standalone_pages
    ]
    _write_journal(
        wiki_root,
        plan_id=resolved_plan_id,
        uid=plan.uid,
        parent_path=computed.parent_path,
        pre_content_hash=plan.content_hash,
        new_paths=new_paths,
    )

    written: list[Path] = []
    restored: list[TargetPageEdit] = []
    try:
        for sp in computed.sub_pages:
            atomic_write_text(sp.path, sp.text)
            written.append(sp.path)
        for standalone in computed.standalone_pages:
            atomic_write_text(standalone.path, standalone.text)
            written.append(standalone.path)
        for target in computed.targets:
            atomic_write_text(target.path, target.text)
            restored.append(target)
        atomic_write_text(computed.parent_path, computed.parent_text)
    except Exception:
        # An existing target is NOT a pure addition, so unlinking is not the
        # reversal: its own pre-image is already in hand
        # (:attr:`TargetPageEdit.pre_text`), which is what makes a failure
        # part-way through the target run recoverable in process rather than
        # only through the journal.
        for target in reversed(restored):
            try:
                atomic_write_text(target.path, target.pre_text)
            except OSError:  # pragma: no cover - defensive
                pass
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        _remove_journal(wiki_root)
        raise

    # AC6: the relation is set through page_relations.py's own writer, with
    # `via="attach"`, AFTER the parent has its final body -- `set_relation`
    # re-reads both pages from disk and edits only the breadcrumb and the
    # Sub-pages block, so composing it on top of the final body is what
    # keeps the attach from changing any other body text. Every refusal it
    # can make was already pre-checked by `_attachment_issues`; a residual
    # one is reported as a post-write issue rather than silently dropped.
    #
    # **A refused attach still leaves `ok=True`, deliberately** (Seer
    # finding on PR athenaeum#2579, considered and kept). By the time this
    # loop runs every page has been written and the writes are correct;
    # `post_write_issues` is this module's established channel for "the
    # write happened, and here is what is off about it" -- the equally
    # serious `post-write-byte-mismatch` and `post-write-dangling-marker`
    # cases already report that way. Flipping `ok` to False here would be
    # actively worse: `decision_answers` closes the queue item only on
    # success, so the item would stay open over a page that HAS been
    # rewritten, and the next answer would be refused by the content-hash
    # pin -- an item nobody can ever discharge. The honest reporting fix is
    # the ledger one below (only landed attachments are recorded), so an
    # undo never chases a relation that was never set.
    attach_issues: list[PlanIssue] = []
    attach_pages: list[dict[str, Any]] = []
    attached: list[Attachment] = []
    for attachment in computed.attachments:
        pair_pre: dict[str, tuple[Path, str]] = {}
        for pair_uid in (attachment.child_uid, attachment.parent_uid):
            pair_path = index.get_by_uid(pair_uid)
            if pair_path is not None:
                pair_pre[pair_uid] = (pair_path, pair_path.read_text(encoding="utf-8"))
        try:
            set_relation(
                wiki_root,
                child_uid=attachment.child_uid,
                parent_uid=attachment.parent_uid,
                via=attachment.via,
            )
        except (RelationError, ValueError) as exc:
            attach_issues.append(
                PlanIssue(
                    "attach-refused",
                    f"{attachment.child_uid} under {attachment.parent_uid}: {exc}",
                )
            )
            continue
        attached.append(attachment)
        for pair_uid, (pair_path, pre_text) in pair_pre.items():
            _, post_body = parse_frontmatter(pair_path.read_text(encoding="utf-8"))
            attach_pages.append(
                {
                    "uid": pair_uid,
                    "path": str(pair_path),
                    "pre_content_hash": page_content_hash(parse_frontmatter(pre_text)[1]),
                    "post_content_hash": page_content_hash(post_body),
                    "pre_blob_id": _git_blob_id(pre_text.encode("utf-8")),
                    "units": [],
                }
            )
    _remove_journal(wiki_root)

    # Issue athenaeum#2550 AC6: the plan just applied is exactly what an
    # open oversize-family notice for this page was asking a human to
    # arrange -- archive it now rather than leaving it open over a page
    # that has already been restructured. Covers both this function's
    # callers: the operator-approved ``recompile apply`` CLI path AND the
    # athenaeum#2549 nightly auto-apply path, since both land here.
    _archive_oversize_notices_for_applied_plan(
        wiki_root, name=str(page_meta.get("name") or ""), plan_id=resolved_plan_id
    )

    # AC5/AC6: re-run the conservation and marker-resolution checks AGAINST
    # THE RE-READ BYTES -- not the in-memory values already trusted above --
    # so a defect in the write path itself (encoding, atomic_write_text) is
    # caught rather than assumed away.
    units_by_id_for_postcheck = {unit.unit_id: unit for unit in segment.units}
    post_issues: list[PlanIssue] = list(attach_issues)
    reread_parent = computed.parent_path.read_text(encoding="utf-8")
    _, reread_parent_body = parse_frontmatter(reread_parent)
    if not computed.attachments and reread_parent != computed.parent_text:
        # Skipped when an attach ran: `set_relation` legitimately rewrote
        # the parent's breadcrumb or Sub-pages block on top of the body this
        # function wrote, so a byte comparison against the pre-attach text
        # would report every successful attach as a write-path defect. The
        # unit-presence and marker-resolution checks below still run, and
        # they are the ones that would catch content loss.
        post_issues.append(PlanIssue("post-write-byte-mismatch", plan.uid))
    for unit_id in computed.parent_unit_ids:
        if units_by_id_for_postcheck[unit_id].text not in reread_parent_body:
            post_issues.append(PlanIssue("post-write-unit-missing", unit_id))
    marker_check_parent = check_marker_resolution(reread_parent_body)
    if not marker_check_parent.ok:
        post_issues.append(PlanIssue("post-write-dangling-marker", plan.uid))

    for sp in computed.sub_pages:
        reread_text = sp.path.read_text(encoding="utf-8")
        _, reread_body = parse_frontmatter(reread_text)
        if reread_text != sp.text:
            post_issues.append(PlanIssue("post-write-byte-mismatch", sp.uid))
        for unit_id in sp.child_unit_ids:
            if renormalise_heading(units_by_id_for_postcheck[unit_id].text) not in reread_body:
                post_issues.append(PlanIssue("post-write-unit-missing", unit_id))
        marker_check = check_marker_resolution(reread_body)
        if not marker_check.ok:
            post_issues.append(PlanIssue("post-write-dangling-marker", sp.uid))

    # Issue athenaeum#2548: the same three post-write checks over every
    # standalone page and every move target, against the RE-READ bytes.
    for standalone in computed.standalone_pages:
        reread_text = standalone.path.read_text(encoding="utf-8")
        _, reread_body = parse_frontmatter(reread_text)
        if reread_text != standalone.text:
            post_issues.append(PlanIssue("post-write-byte-mismatch", standalone.uid))
        for section in standalone.inserted_texts:
            if section not in reread_body:
                post_issues.append(PlanIssue("post-write-unit-missing", standalone.uid))
        if not check_marker_resolution(reread_body).ok:
            post_issues.append(PlanIssue("post-write-dangling-marker", standalone.uid))

    target_post_hashes: dict[str, str] = {}
    attach_touched = {a.child_uid for a in computed.attachments} | {
        a.parent_uid for a in computed.attachments
    }
    for target in computed.targets:
        reread_text = target.path.read_text(encoding="utf-8")
        _, reread_body = parse_frontmatter(reread_text)
        target_post_hashes[target.uid] = page_content_hash(reread_body)
        if target.uid not in attach_touched and reread_text != target.text:
            post_issues.append(PlanIssue("post-write-byte-mismatch", target.uid))
        for section in target.inserted_texts:
            if section not in reread_body:
                post_issues.append(PlanIssue("post-write-unit-missing", target.uid))
        if not check_marker_resolution(reread_body).ok:
            post_issues.append(PlanIssue("post-write-dangling-marker", target.uid))

    pre_parent_hash = plan.content_hash
    post_parent_hash = page_content_hash(reread_parent_body)
    ledger_record = _build_ledger_record(
        plan_id=resolved_plan_id,
        uid=plan.uid,
        computed=computed,
        pre_parent_hash=pre_parent_hash,
        post_parent_hash=post_parent_hash,
        target_post_hashes=target_post_hashes,
        attach_pages=attach_pages,
        # Only the attachments that actually LANDED (Seer finding on PR
        # athenaeum#2579). The ledger is what `page_recompile_undo` detaches
        # from, so recording a refused attach would send an undo chasing a
        # relation that was never set -- harmless today (the detach tolerates
        # it) but a false audit record either way, and the ledger's whole job
        # is to say what happened.
        attachments=tuple(attached),
    )
    target_ledger = (
        ledger_path if ledger_path is not None else default_page_recompile_ledger_path(wiki_root)
    )
    _append_record(target_ledger, ledger_record)

    knowledge_root = wiki_root.parent
    snapshot_sha = FilesystemStore(knowledge_root, config or {}).snapshot(
        f"page-recompile apply {plan.uid} ({resolved_plan_id})"
    )

    return ApplyOutcome(
        ok=True,
        reason="applied",
        plan_id=resolved_plan_id,
        uid=plan.uid,
        created_uids=tuple(sp.uid for sp in computed.sub_pages)
        + tuple(sp.uid for sp in computed.standalone_pages),
        snapshot_sha=snapshot_sha,
        ledger_record_id=resolved_plan_id,
        post_write_issues=tuple(post_issues),
    )


__all__ = [
    "APPLY_KIND",
    "APPROVING_VERDICTS",
    "JOURNAL_FILENAME",
    "AliasMove",
    "ApplyOutcome",
    "Attachment",
    "ComputedApply",
    "MergeAccounting",
    "NewStandalonePage",
    "NewSubPage",
    "TargetPageEdit",
    "access_widening_reason",
    "compute_apply",
    "relocation_note",
    "renormalise_heading",
    "rollback_stale_journal",
    "write_apply",
]
