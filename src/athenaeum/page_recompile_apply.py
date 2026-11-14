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
reachable through the undo snapshot. This is the fix for
:func:`athenaeum.tiers._perform_oversize_page_split`'s two defects named in
the issue: that function replaces the parent body with a fixed notice plus
a link list, and it slices by heading offset so a trailing footnote block
lands only on the last child.

**Compute-first, write-last, same discipline as
:func:`athenaeum.tiers._perform_oversize_page_split`.** Every sub-page's
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

**Out of scope (per the issue).** ``move``/``attach``/``attach-self-under``
(cross-page actions -- :func:`compute_apply` refuses any plan whose
:attr:`~athenaeum.page_recompile_plan.RecompilePlan.scope` is not
:data:`~athenaeum.page_recompile_plan.SCOPE_STRUCTURE_ONLY`), standalone
pages, undo, the nightly phase, and auto-apply.

Layering: L4 domain/pipeline, the same layer as its siblings
:mod:`athenaeum.page_recompile` and :mod:`athenaeum.page_recompile_plan`
(same-layer imports are permitted by ``tests/test_layer_boundary.py``).
Imports :mod:`athenaeum.page_relations` (L3) for the relation primitives
this module reuses rather than duplicates, :mod:`athenaeum.config` (L2),
:mod:`athenaeum.atomic_io`, :mod:`athenaeum.footnote_markers` and
:mod:`athenaeum.models` (all L1/L0), and :mod:`athenaeum.store` (L0/L1) for
:class:`~athenaeum.store.FilesystemStore`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_page_recompile_merge_near_duplicates,
    resolve_page_recompile_min_parent_chars,
    resolve_page_size_threshold_chars,
)
from athenaeum.footnote_markers import INLINE_MARKER_RE, parse_footnote_definitions
from athenaeum.models import (
    EntityIndex,
    TokenUsage,
    WikiEntity,
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
    read_page_recompile_ledger,
    split_disposition,
    split_summary_sentences,
    verify_summary,
)
from athenaeum.page_relations import (
    ROLE_CHILD,
    ChildRef,
    _children_display_rows,
    _index_pages_by_uid,
    _is_child_relation_row,
    _related_rows,
    apply_breadcrumb,
    apply_sub_pages_section,
    read_relations,
)
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
    """
    issues: list[PlanIssue] = []
    units_by_id = {unit.unit_id: unit for unit in segment.units}
    groups_by_key = {group.key: group for group in plan.groups}
    merges_by_group = {merge.group: merge for merge in plan.merges}

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
        else:
            # Cross-page verbs (move/standalone/attach/attach-self-under)
            # cannot appear in a structure-only plan by construction
            # (classify_plan_scope); a plan this function is even called
            # with carries one anyway only if something upstream is broken.
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

    # AC4: new children linked via the page_relations.py writer's shape
    # (`via="split"`), folded together with any pre-existing children in
    # ONE write rather than one `set_relation` call per child -- a repeated
    # double-write per child would rewrite the parent N times with
    # intermediate (not-yet-final) content, breaking the "parent written
    # once, last, with its final body" contract this module (and
    # tiers.py:_perform_oversize_page_split before it) relies on.
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

    # AC2: frontmatter byte-identical outside exactly these four keys.
    new_meta: dict[str, Any] = dict(page_meta)
    new_meta["updated"] = today
    new_meta["description"] = _derive_description(plan.summary)
    new_meta["related"] = new_related
    threshold = resolve_page_size_threshold_chars(config)
    if len(new_parent_body) <= threshold:
        new_meta.pop("needs_split", None)
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
) -> dict[str, Any]:
    """The AC9 record: pre/post content hash and the pre-image git blob id
    for every touched page, the created uids, which units landed where, and
    the merged groups' original unit ids/hashes."""
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
    return {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": APPLY_KIND,
        "id": plan_id,
        "ts": now_iso(),
        "uid": uid,
        "created_uids": [sp.uid for sp in computed.sub_pages],
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
    }


def write_apply(
    wiki_root: Path,
    *,
    uid: str = "",
    plan_id: str = "",
    client: Any = None,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    now: date | None = None,
) -> ApplyOutcome:
    """Apply ONE structure-only recompile plan end to end (issue athenaeum#2540).

    Rolls back a stale journal first (AC10), resolves and re-validates the
    plan (AC1, :func:`compute_apply`), writes every sub-page then the parent
    LAST under journal protection, re-verifies the written pages, appends
    the AC9 ledger record, and makes one snapshot commit
    (:meth:`athenaeum.store.FilesystemStore.snapshot`).
    """
    rollback_stale_journal(wiki_root)

    plan, resolved_plan_id, error = _load_plan_for_apply(
        wiki_root, uid=uid, plan_id=plan_id, ledger_path=ledger_path
    )
    if plan is None:
        return ApplyOutcome(ok=False, reason=error, plan_id=resolved_plan_id, uid=uid)

    if plan.scope != SCOPE_STRUCTURE_ONLY:
        return ApplyOutcome(
            ok=False,
            reason=f"plan scope is {plan.scope!r}, not structure-only",
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

    new_paths = [sp.path for sp in computed.sub_pages]
    _write_journal(
        wiki_root,
        plan_id=resolved_plan_id,
        uid=plan.uid,
        parent_path=computed.parent_path,
        pre_content_hash=plan.content_hash,
        new_paths=new_paths,
    )

    written: list[Path] = []
    try:
        for sp in computed.sub_pages:
            atomic_write_text(sp.path, sp.text)
            written.append(sp.path)
        atomic_write_text(computed.parent_path, computed.parent_text)
    except Exception:
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        _remove_journal(wiki_root)
        raise
    _remove_journal(wiki_root)

    # AC5/AC6: re-run the conservation and marker-resolution checks AGAINST
    # THE RE-READ BYTES -- not the in-memory values already trusted above --
    # so a defect in the write path itself (encoding, atomic_write_text) is
    # caught rather than assumed away.
    units_by_id_for_postcheck = {unit.unit_id: unit for unit in segment.units}
    post_issues: list[PlanIssue] = []
    reread_parent = computed.parent_path.read_text(encoding="utf-8")
    _, reread_parent_body = parse_frontmatter(reread_parent)
    if reread_parent != computed.parent_text:
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

    pre_parent_hash = plan.content_hash
    post_parent_hash = page_content_hash(reread_parent_body)
    ledger_record = _build_ledger_record(
        plan_id=resolved_plan_id,
        uid=plan.uid,
        computed=computed,
        pre_parent_hash=pre_parent_hash,
        post_parent_hash=post_parent_hash,
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
        created_uids=tuple(sp.uid for sp in computed.sub_pages),
        snapshot_sha=snapshot_sha,
        ledger_record_id=resolved_plan_id,
        post_write_issues=tuple(post_issues),
    )


__all__ = [
    "APPLY_KIND",
    "JOURNAL_FILENAME",
    "ApplyOutcome",
    "ComputedApply",
    "MergeAccounting",
    "NewSubPage",
    "compute_apply",
    "renormalise_heading",
    "rollback_stale_journal",
    "write_apply",
]
