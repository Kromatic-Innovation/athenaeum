# SPDX-License-Identifier: Apache-2.0
"""Page recompile: the approval queue for a drafted plan (issue
athenaeum#2547, slice S6 of the page-recompile design, athenaeum#2525).

:mod:`athenaeum.page_recompile_plan` (S3) drafts a plan and appends it to
``wiki/_page_recompiles.jsonl`` as a shadow record pinned to the page's
content hash. :mod:`athenaeum.page_recompile_apply` (S4) is the write side.
THIS module is the gate between them: it raises one ``page-recompile``
decision item per plan, lists the unanswered ones, and records the
operator's answer.

**One ledger, not a second one.** Every record here goes into the SAME
append-only file :mod:`athenaeum.page_recompile_plan` already owns (see
:func:`~athenaeum.page_recompile_plan.default_page_recompile_ledger_path`),
under three new kinds alongside its ``label``/``plan`` and the
``apply``/``undo`` kinds its siblings write. That is deliberate: the
suppression rule below is a comparison between a REJECTED plan's pinned
content hash and the page's live body, exactly the shape
:func:`~athenaeum.page_recompile_plan.undone_page_skip_reason` already
implements against ``undo`` records, and splitting the two halves of that
comparison across two files would mean two tolerant readers, two paths and
two migration stories for one question.

**The decision id IS the plan id.** A plan id
(:meth:`~athenaeum.page_recompile_plan.RecompilePlan.plan_id`) is a hash
over the page identity, its CONTENT HASH and every decision in the plan --
so an answer can never be ambiguous about which draft over which bytes it
answers, and a page edited after the plan was drafted produces a different
id on the next pass rather than colliding with the stale one.

**What the item carries, and what it must never carry** (athenaeum#2547's
own AC): the plan OUTLINE -- group names, unit counts, sizes and
first-line excerpts -- and never a page body.
:func:`build_plan_outline` is the one place that projection happens, and it
reads unit text only to cut a single-line excerpt from it
(:data:`EXCERPT_MAX_CHARS`). In particular it drops
:attr:`~athenaeum.page_recompile_plan.MergedSection.text`, the model-written
merged prose, which is the one genuinely body-sized field a plan record
carries. Shaped as a flat dict of named counts plus one row per group so
athenaeum#2489's presentation template is a drop-in over it later.

**Three verdicts** (:data:`VERDICTS`): ``approve``,
``approve-structure-only`` and ``reject``. The appliers live in
:mod:`athenaeum.decision_answers`; this module only reads and writes the
ledger, so it stays free of any edge toward the decision machinery.

Layering: L4 domain/pipeline, the same layer as its siblings
:mod:`athenaeum.page_recompile`, :mod:`athenaeum.page_recompile_plan`,
:mod:`athenaeum.page_recompile_apply` and
:mod:`athenaeum.page_recompile_undo` (same-layer imports are permitted by
``tests/test_layer_boundary.py``). It imports
:mod:`athenaeum.page_recompile_plan` (L4, for the ledger it owns) and
:mod:`athenaeum.store` (L1) and nothing else -- deliberately NOT
:mod:`athenaeum.decisions` or :mod:`athenaeum.decision_answers`, which
import THIS module (``tests/test_import_graph_acyclic.py`` allows no
cycle).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from athenaeum.page_recompile_plan import (
    PAGE_RECOMPILE_LEDGER_VERSION,
    PLAN_KIND,
    RecompilePlan,
    _append_record,
    default_page_recompile_ledger_path,
    page_content_hash,
    read_page_recompile_ledger,
    split_disposition,
)
from athenaeum.store import now_iso

log = logging.getLogger(__name__)

#: Ledger record kind for a plan raised as a decision item.
QUEUE_KIND = "queued"

#: Ledger record kinds for the three terminal answers. ``approve`` covers
#: BOTH approving verdicts -- the record carries the verdict verbatim, so a
#: reader can still tell ``approve`` from ``approve-structure-only``, but
#: "is this item still open?" needs one kind, not two.
APPROVE_KIND = "approve"
REJECT_KIND = "reject"

#: Ledger record kind for an answer that could not be carried out because
#: the page had MOVED since the plan was drafted (athenaeum#2547 AC8). It
#: resolves the item -- the stale draft leaves the queue, since no answer to
#: it can ever be applied -- but, unlike :data:`REJECT_KIND`, it records NO
#: suppression, so the next nightly pass re-plans the page. A third kind
#: rather than a flag on an ``approve``/``reject`` record because the
#: question "may this page be planned again?" is answered by which kind is
#: present, and a flag would make that read conditional.
STALE_KIND = "stale"

#: Every kind that CLOSES a queued item.
RESOLUTION_KINDS: frozenset[str] = frozenset({APPROVE_KIND, REJECT_KIND, STALE_KIND})

#: The decision type's own tag, used by :mod:`athenaeum.decisions`,
#: :mod:`athenaeum.decision_framing` and :mod:`athenaeum.decision_answers`.
#: Declared here, the module that owns the store, so those three agree by
#: import rather than by three string literals.
DECISION_TYPE = "page-recompile"

#: The verdict vocabulary (athenaeum#2547 AC7).
VERDICT_APPROVE = "approve"
VERDICT_APPROVE_STRUCTURE_ONLY = "approve-structure-only"
VERDICT_REJECT = "reject"
VERDICTS: tuple[str, ...] = (
    VERDICT_APPROVE,
    VERDICT_APPROVE_STRUCTURE_ONLY,
    VERDICT_REJECT,
)

#: How much of a unit's first line an outline excerpt may carry. One line,
#: truncated: enough for an operator to recognise which part of the page a
#: group is, far too little to be a body.
EXCERPT_MAX_CHARS = 120


# ---------------------------------------------------------------------------
# The outline (AC7: group names, unit counts, sizes, first-line excerpts --
# and never a page body).
# ---------------------------------------------------------------------------


def _excerpt(text: str) -> str:
    """The first non-blank line of *text*, collapsed and truncated to
    :data:`EXCERPT_MAX_CHARS`."""
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if len(line) <= EXCERPT_MAX_CHARS:
            return line
        return line[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return ""


def build_plan_outline(
    plan: RecompilePlan,
    units: "Sequence[Any]",
    *,
    page_name: str = "",
    page_chars: int = 0,
) -> dict[str, Any]:
    """Project *plan* down to the decision item's outline (athenaeum#2547 AC7).

    *units* is the segmenter's own
    :attr:`~athenaeum.page_recompile.SegmentResult.units` for the page the
    plan was drafted over -- the ONLY source of unit sizes and excerpts
    (a plan record carries unit IDs, not unit text). Passed in rather than
    re-segmented here so this function makes no assumption about whether the
    page on disk still matches the plan.

    Carries, per the AC: the group NAMES (and their one-line descriptions,
    which the planner already wrote), the unit COUNT and total char SIZE per
    destination, and one FIRST-LINE EXCERPT per group. Carries NO page body:
    every merged section's model-written ``text`` is dropped, and an excerpt
    is capped at :data:`EXCERPT_MAX_CHARS` of a single line.
    """
    by_id = {getattr(unit, "unit_id", ""): unit for unit in units}

    def _unit_chars(unit_id: str) -> int:
        unit = by_id.get(unit_id)
        return len(getattr(unit, "text", "") or "") if unit is not None else 0

    def _unit_excerpt(unit_id: str) -> str:
        unit = by_id.get(unit_id)
        return _excerpt(getattr(unit, "text", "") or "") if unit is not None else ""

    # Destination key -> the unit ids routed there, in page order.
    buckets: dict[str, list[str]] = {}
    duplicates = 0
    for unit_id, disposition in plan.dispositions:
        verb, argument = split_disposition(disposition)
        if verb == "duplicate":
            duplicates += 1
            continue
        key = argument or verb
        buckets.setdefault(key, []).append(unit_id)

    merged_groups = {merge.group for merge in plan.merges}
    named = {group.key: group for group in plan.groups}

    groups: list[dict[str, Any]] = []
    parent: dict[str, Any] = {"units": 0, "chars": 0, "excerpt": ""}
    for key, unit_ids in buckets.items():
        chars = sum(_unit_chars(unit_id) for unit_id in unit_ids)
        excerpt = next(
            (e for e in (_unit_excerpt(unit_id) for unit_id in unit_ids) if e), ""
        )
        if key == "parent":
            parent = {"units": len(unit_ids), "chars": chars, "excerpt": excerpt}
            continue
        group = named.get(key)
        groups.append(
            {
                "key": key,
                "name": group.name if group is not None else key,
                "description": group.description if group is not None else "",
                "units": len(unit_ids),
                "chars": chars,
                "excerpt": excerpt,
                "merged": key in merged_groups,
            }
        )
    groups.sort(key=lambda row: str(row["key"]))

    return {
        "uid": plan.uid,
        "page_name": page_name,
        "page_chars": page_chars,
        "content_hash": plan.content_hash,
        "scope": plan.scope,
        "parent": parent,
        "groups": groups,
        "group_count": len(groups),
        "unit_count": len(plan.dispositions),
        "duplicate_count": duplicates,
        "page_actions": list(plan.page_actions),
    }


# ---------------------------------------------------------------------------
# Raising and reading.
# ---------------------------------------------------------------------------


def _ledger_target(wiki_root: Path, ledger_path: Path | None) -> Path:
    return (
        ledger_path
        if ledger_path is not None
        else default_page_recompile_ledger_path(wiki_root)
    )


def queue_plan_for_approval(
    wiki_root: Path,
    *,
    plan_id: str,
    plan: RecompilePlan,
    outline: Mapping[str, Any],
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append the :data:`QUEUE_KIND` record that makes *plan* a pending
    ``page-recompile`` decision item, and return it.

    Idempotent by plan id: re-queueing an already-queued, still-unanswered
    plan appends nothing and returns the existing record. The nightly phase
    relies on that -- a plan over unchanged bytes has the same id, so a
    second night that re-reaches the same page must not raise the same
    question twice.
    """
    records = read_page_recompile_ledger(wiki_root, ledger_path=ledger_path)
    existing = next(
        (
            r
            for r in records
            if r.get("kind") == QUEUE_KIND and str(r.get("id")) == plan_id
        ),
        None,
    )
    if existing is not None and plan_id not in _resolved_ids(records):
        return existing

    record: dict[str, Any] = {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": QUEUE_KIND,
        "id": plan_id,
        "ts": now_iso(now),
        "created_at": now_iso(now),
        "uid": plan.uid,
        "content_hash": plan.content_hash,
        "scope": plan.scope,
        "outline": dict(outline),
    }
    _append_record(_ledger_target(wiki_root, ledger_path), record)
    log.info(
        "athenaeum#2547: queued page-recompile plan %s on page %s for approval",
        plan_id,
        plan.uid,
    )
    return record


def _resolved_ids(records: "Sequence[Mapping[str, Any]]") -> set[str]:
    return {
        str(r.get("id")) for r in records if r.get("kind") in RESOLUTION_KINDS
    }


def list_pending_page_recompile_plans(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Queued plans with no :data:`RESOLUTION_KINDS` record, oldest first.

    Same shape and same tolerant-read contract as
    :func:`athenaeum.auto_apply_proposals.list_pending_auto_apply_threshold_proposals`.
    De-duplicated by plan id (the ledger is append-only, so the LAST record
    for an id wins, matching
    :func:`~athenaeum.page_recompile_plan.find_plan_record`) so a
    re-queued id can never be counted twice against
    ``max_open_items``.
    """
    records = read_page_recompile_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    pending: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != QUEUE_KIND:
            continue
        plan_id = str(record.get("id"))
        if plan_id in resolved:
            continue
        pending[plan_id] = record
    return list(pending.values())


def find_queued_plan(
    wiki_root: Path, *, plan_id: str, ledger_path: Path | None = None
) -> dict[str, Any] | None:
    """The LAST :data:`QUEUE_KIND` record for *plan_id*, answered or not."""
    match: dict[str, Any] | None = None
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") == QUEUE_KIND and str(record.get("id")) == plan_id:
            match = record
    return match


def plans_awaiting_approval(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> int | None:
    """How many ``page-recompile`` items are unanswered, or ``None`` when no
    ledger exists at all (issue athenaeum#2547 AC9).

    ``None`` rather than ``0`` for an absent ledger, honouring
    :data:`athenaeum.status.PAGE_RECOMPILE_NOT_AVAILABLE`'s own contract:
    "nothing has ever proposed a plan" and "the queue is empty" are
    different facts, and a starvation signal must not be shown as the
    healthy-looking one. An EXISTING ledger with no open item is a real,
    measured ``0``.
    """
    target = _ledger_target(wiki_root, ledger_path)
    if not target.exists():
        return None
    return len(list_pending_page_recompile_plans(wiki_root, ledger_path=ledger_path))


#: The ledger kinds that count as the pass having made PROGRESS on a night:
#: a plan drafted, a plan raised for approval, or a plan applied. Deliberately
#: not ``label`` (a cached labelling call is not progress on a page) and not
#: ``undo`` (that is an operator reversing progress).
PROGRESS_KINDS: frozenset[str] = frozenset({PLAN_KIND, QUEUE_KIND, "apply"})


def nights_since_progress(
    wiki_root: Path,
    *,
    now: date | None = None,
    ledger_path: Path | None = None,
) -> int | None:
    """Whole days since the page-recompile pass last made progress, or
    ``None`` when it never has (issue athenaeum#2547 AC9).

    Progress is any :data:`PROGRESS_KINDS` record. Read off the ledger's own
    timestamps rather than from a per-run marker: the ledger already records
    every event that counts as progress, and a second "the phase ran
    tonight" record would be a second source of truth for the same fact.

    ``None`` for a ledger with no progress record is the
    :data:`athenaeum.status.PAGE_RECOMPILE_NOT_AVAILABLE` sentinel's case,
    for the reason :func:`plans_awaiting_approval` gives. A record whose
    timestamp cannot be parsed is ignored rather than treated as today.
    """
    latest: date | None = None
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") not in PROGRESS_KINDS:
            continue
        stamp = record.get("ts") or record.get("created_at")
        if not isinstance(stamp, str) or not stamp:
            continue
        try:
            seen = date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        if latest is None or seen > latest:
            latest = seen
    if latest is None:
        return None
    today = now or date.today()
    return max(0, (today - latest).days)


# ---------------------------------------------------------------------------
# Suppression (AC8: a rejected plan skips the page until its content hash
# changes).
# ---------------------------------------------------------------------------


def rejected_page_skip_reason(
    wiki_root: Path,
    *,
    uid: str,
    body: str,
    ledger_path: Path | None = None,
) -> str:
    """Why *uid* must NOT be planned again yet, or ``""`` (issue
    athenaeum#2547 AC8).

    An operator who rejected a plan has made a judgement about that page as
    it stands. Without this check the very next nightly pass would read the
    same oversized page, draft the same plan and ask the same question
    again. A :data:`REJECT_KIND` record pins the content hash the rejection
    was about; this function skips the page while its live body still hashes
    to that value.

    **A hash comparison, never a timestamp or a permanent block** -- the
    same rule, for the same reason, as
    :func:`~athenaeum.page_recompile_plan.undone_page_skip_reason`: the
    moment the page's content differs the hash stops matching and planning
    resumes on its own. The LAST matching record wins, and a reject over a
    hash the page no longer holds clears the skip rather than latching it.
    """
    current = page_content_hash(body)
    reason = ""
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") != REJECT_KIND or record.get("uid") != uid:
            continue
        pinned = record.get("content_hash")
        if isinstance(pinned, str) and pinned and pinned == current:
            reason = (
                f"a page-recompile plan ({record.get('id') or 'unknown'}) was "
                "rejected for this page and it has not changed since; it is "
                "skipped until its content hash differs"
            )
        else:
            reason = ""
    return reason


# ---------------------------------------------------------------------------
# Answering.
# ---------------------------------------------------------------------------


class UnknownPageRecompileItem(ValueError):
    """No queued item carries the given plan id."""


class PageRecompileItemAlreadyResolved(ValueError):
    """The queued item carrying the given plan id has already been answered."""


def _require_open(
    wiki_root: Path, plan_id: str, ledger_path: Path | None
) -> dict[str, Any]:
    records = read_page_recompile_ledger(wiki_root, ledger_path=ledger_path)
    queued = [
        r for r in records if r.get("kind") == QUEUE_KIND and str(r.get("id")) == plan_id
    ]
    if not queued:
        raise UnknownPageRecompileItem(
            f"unknown page-recompile item id: {plan_id!r}"
        )
    if plan_id in _resolved_ids(records):
        raise PageRecompileItemAlreadyResolved(
            f"page-recompile item already resolved: {plan_id!r}"
        )
    return queued[-1]


def _record_resolution(
    wiki_root: Path,
    *,
    kind: str,
    plan_id: str,
    verdict: str,
    note: str,
    ledger_path: Path | None,
    now: datetime | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item = _require_open(wiki_root, plan_id, ledger_path)
    record: dict[str, Any] = {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": kind,
        "id": plan_id,
        "ts": now_iso(now),
        "answered_at": now_iso(now),
        "uid": item.get("uid"),
        "content_hash": item.get("content_hash"),
        "verdict": verdict,
        "note": note,
    }
    if extra:
        record.update(dict(extra))
    _append_record(_ledger_target(wiki_root, ledger_path), record)
    return record


def record_page_recompile_approval(
    wiki_root: Path,
    *,
    plan_id: str,
    verdict: str = VERDICT_APPROVE,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Close a queued item as APPROVED, recording *verdict* verbatim.

    Appended only once the apply itself has succeeded -- the caller
    (:func:`athenaeum.decision_answers._apply_page_recompile_answer`) runs
    the applier first, so a refused apply leaves the item open and
    answerable again rather than closed with nothing done.

    Raises :class:`UnknownPageRecompileItem` /
    :class:`PageRecompileItemAlreadyResolved`.
    """
    return _record_resolution(
        wiki_root,
        kind=APPROVE_KIND,
        plan_id=plan_id,
        verdict=verdict,
        note=note,
        ledger_path=ledger_path,
        now=now,
        extra=extra,
    )


def record_page_recompile_rejection(
    wiki_root: Path,
    *,
    plan_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close a queued item as REJECTED, suppressing the page until its
    content hash changes (:func:`rejected_page_skip_reason`)."""
    record = _record_resolution(
        wiki_root,
        kind=REJECT_KIND,
        plan_id=plan_id,
        verdict=VERDICT_REJECT,
        note=note,
        ledger_path=ledger_path,
        now=now,
    )
    log.info(
        "athenaeum#2547: rejected page-recompile plan %s on page %s; the page is "
        "skipped until its content hash changes",
        plan_id,
        record.get("uid"),
    )
    return record


def record_page_recompile_stale(
    wiki_root: Path,
    *,
    plan_id: str,
    verdict: str,
    reason: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close a queued item as STALE: the page moved after the plan was
    drafted, so the answer was not applied and the page will be re-planned
    (issue athenaeum#2547 AC8).

    Records NO suppression -- that is the whole difference from
    :func:`record_page_recompile_rejection`, and the reason this is its own
    kind.
    """
    record = _record_resolution(
        wiki_root,
        kind=STALE_KIND,
        plan_id=plan_id,
        verdict=verdict,
        note=note,
        ledger_path=ledger_path,
        now=now,
        extra={"reason": reason},
    )
    log.info(
        "athenaeum#2547: page-recompile plan %s on page %s was not applied (%s); "
        "the page will be re-planned",
        plan_id,
        record.get("uid"),
        reason,
    )
    return record


__all__ = [
    "APPROVE_KIND",
    "DECISION_TYPE",
    "EXCERPT_MAX_CHARS",
    "PROGRESS_KINDS",
    "QUEUE_KIND",
    "REJECT_KIND",
    "RESOLUTION_KINDS",
    "STALE_KIND",
    "VERDICTS",
    "VERDICT_APPROVE",
    "VERDICT_APPROVE_STRUCTURE_ONLY",
    "VERDICT_REJECT",
    "PageRecompileItemAlreadyResolved",
    "UnknownPageRecompileItem",
    "build_plan_outline",
    "find_queued_plan",
    "list_pending_page_recompile_plans",
    "nights_since_progress",
    "plans_awaiting_approval",
    "queue_plan_for_approval",
    "record_page_recompile_approval",
    "record_page_recompile_rejection",
    "record_page_recompile_stale",
    "rejected_page_skip_reason",
]
