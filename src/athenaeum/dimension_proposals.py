# SPDX-License-Identifier: Apache-2.0
"""Dimension-proposal drafter (issue athenaeum#2015, athenaeum#719 Plan step 3).

The self-tuning loop's second stage: :mod:`athenaeum.signal_mining` detects a
RECURRING typed shape of missing dimensions; this module turns one
:class:`~athenaeum.signal_mining.MinedShape` that crossed the recurrence
threshold into a queue item a human can ratify in one sitting — never a raw
shape dump.

**Fully deterministic — no LLM, no client, no prompt.** Unlike
:mod:`athenaeum.rule_proposals` (which makes one drafting call per shape),
this drafter derives every field of a proposal mechanically from the
:class:`~athenaeum.signal_mining.ShapeKey` the detector already computed. An
LLM may *summarize* a proposal for a human later (a future child), but it
must never sit on this trigger path (issue athenaeum#719 AC's own text) — this
module makes that true by construction, not by convention.

**Drafted per missing dimension, not per shape.** A triggered shape's
``missing_dimensions`` tuple may name more than one axis; this module drafts
one proposal item per ``(shape, dimension name)`` pair, each independently
idempotent (a pending or rejected id is never re-drafted — mirrors
:func:`athenaeum.rule_proposals.run_rule_proposal_detection`'s per-shape
idempotency, applied at the finer grain the AC's singular "name"/"kind"
fields require).

**The ask-budget bound, enforced here, not at ratification.** A proposal's
backfill plan marks each missing dimension as ``"auto"`` (populate from
provenance already recorded elsewhere) or ``"ask"`` (a human must answer a
coordinate question per affected pair). The number of asks a drafted
proposal would cost is bounded against
:func:`athenaeum.config.resolve_decisions_budget_items_per_day_max` — the
SAME items/day effort bound :mod:`athenaeum.decision_budget` already polices
for the whole queue (issue athenaeum#1990/athenaeum#717); this module reuses
that resolver rather than inventing a second budget knob. A proposal whose
ask tail would exceed the bound ships ALREADY narrowed (fewer pairs) or
auto-backfill-only — :func:`enforce_ask_budget` is where that happens, and it
runs inside :func:`draft_dimension_proposal` before the proposal is ever
persisted, never discovered later at ratification time.

**Origin-is-provenance (issue athenaeum#714's rule, restated for this
module).** :func:`plan_backfill` NEVER marks the kernel ``scope`` dimension
auto-populate-from-provenance, even when a caller's ``coord_origins`` mapping
names ``scope`` — a page's raw intake ``origin_scope`` is exactly the kind of
convenient-looking signal that must never silently become the ``scope``
coordinate's value (the same hazard athenaeum#1994's ``coord_origins`` field
exists to make auditable, not to license). See :func:`plan_backfill`'s own
docstring and ``tests/test_dimension_proposals.py``'s
``test_backfill_never_auto_populates_scope_from_provenance`` for the
regression this encodes.

**Ledger-only; no resolution path in this module.** Ratifying (approving,
renaming, or rejecting) a drafted proposal is the next child's
``_apply_dimension_proposal_answer`` — out of scope here (see the issue's own
"Out of scope" section). This module only detects, drafts, and appends to
``wiki/_dimension_proposals.jsonl``; :mod:`athenaeum.decisions` reads that
ledger for display, :mod:`athenaeum.decision_framing` frames the item as
visible-but-not-yet-answerable (``answerable_as("dimension-proposal") is
None``), and nothing in this module or those two ever mutates the operator's
live ``dimensions:`` config.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.rule_proposals`
and :mod:`athenaeum.signal_mining`. Imports :mod:`athenaeum.signal_mining`
(L4, for :class:`~athenaeum.signal_mining.MinedShape`/:class:`~athenaeum.
signal_mining.ShapeKey`), :mod:`athenaeum.dimensions` (L1/L2, for
:class:`~athenaeum.dimensions.Dimension`/:data:`~athenaeum.dimensions.SCOPE`/
:class:`~athenaeum.dimensions.DimensionKind`/:class:`~athenaeum.dimensions.
NullMeans` — read-only; this module never calls :func:`~athenaeum.dimensions.
build_registry` or writes to it), :mod:`athenaeum.config` (L2), and
:mod:`athenaeum.store` (L3, for the shared durable-append/now_iso
primitives). :mod:`athenaeum.decisions` imports this module for its mapper;
this module never imports ``decisions`` back, so no cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from athenaeum.config import resolve_decisions_budget_items_per_day_max
from athenaeum.dimensions import SCOPE, DimensionKind, NullMeans
from athenaeum.signal_mining import MinedShape, ShapeKey
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
DIMENSION_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_rule_proposals.jsonl`` /
#: ``_decision_budget_events.jsonl``.
DIMENSION_PROPOSALS_LEDGER_FILENAME = "_dimension_proposals.jsonl"

#: Record kinds. Only ``proposal`` is ever written by this module
#: (resolution kinds belong to the ratification child — see module
#: docstring's "Ledger-only" note); declared here as the single source of
#: truth for the kind literal the next child's writer will need to match.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"
#: Issue athenaeum#2017 (AC5): a pending proposal whose drafted backfill plan
#: cited a since-revoked resolution claim as its "auto" provenance for one
#: axis. Appended, never replacing or removing the ``proposal`` record it
#: names — "stale-marked, never deleted" mirrors
#: :mod:`athenaeum.verdicts`'s ``stale``/``stale_reason`` convention for the
#: verdict ledger, applied here as a distinct event record rather than an
#: in-place field flip, since this ledger's existing records are written
#: once and never rewritten (unlike the verdict ledger's partitions).
STALE_KIND = "stale"

BackfillAction = Literal["auto", "ask"]


def default_dimension_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_dimension_proposals.jsonl``."""
    return Path(wiki_root) / DIMENSION_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    return now_iso(now)


def proposal_item_id(dimension_name: str, key: ShapeKey) -> str:
    """Deterministic id for the ``(dimension_name, ShapeKey)`` pair.

    NOT per-event (mirrors :func:`athenaeum.rule_proposals.proposal_item_id`):
    a proposal's identity is the dimension it would register plus the shape
    it was drafted from, so a rejected (dimension, shape) pair is
    permanently suppressed by set-membership alone — no separate
    suppression index needed.
    """
    payload = json.dumps(
        {
            "dimension_name": dimension_name,
            "verdict_type": key.verdict_type,
            "missing_dimensions": list(key.missing_dimensions),
            "memory_classes": list(key.memory_classes),
            "scopes": list(key.scopes),
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_dimension_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Same tolerant-reader contract as
    :func:`athenaeum.rule_proposals.read_rule_proposals_ledger`.
    """
    target = (
        ledger_path
        if ledger_path is not None
        else default_dimension_proposals_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _kind_ids(records: list[dict[str, Any]], kind: str) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") == kind}


def _resolved_ids(records: list[dict[str, Any]]) -> set[str]:
    return _kind_ids(records, APPROVE_KIND) | _kind_ids(records, REJECT_KIND)


def list_pending_dimension_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Drafted dimension proposals awaiting ratification.

    Excludes any proposal event already carrying a matching approve OR
    reject record — same "unreviewed" filter shape as
    :func:`athenaeum.rule_proposals.list_pending_rule_proposals`. Today
    nothing in this codebase ever writes an approve/reject record to this
    ledger (that is the next child's job), so every proposal here is
    unconditionally pending; this filter exists so that child's writes are
    honored the moment they land, with no change needed on this side.
    """
    records = read_dimension_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


def stale_proposal_ids(records: list[dict[str, Any]]) -> set[str]:
    """The set of proposal ids carrying at least one :data:`STALE_KIND` event."""
    return _kind_ids(records, STALE_KIND)


def mark_proposals_stale_for_decision(
    wiki_root: Path,
    decision_id: str,
    *,
    reason: str,
    ledger_path: Path | None = None,
) -> list[str]:
    """Stale-mark every PENDING proposal whose backfill plan cited *decision_id*
    (issue athenaeum#2017 AC5).

    Called by :func:`athenaeum.resolution_claims.revoke_resolution_claim` when
    a resolution claim is revoked — reuses THIS ledger's existing
    ``proposal``/``approve``/``reject`` event-append shape (never rewriting
    an existing record) by appending one :data:`STALE_KIND` event per
    affected, still-pending proposal id. A proposal already resolved
    (approved/rejected) or already stale-marked for THIS *decision_id* is
    left alone — idempotent, matching every other ledger-write in this
    codebase.

    Returns the list of proposal ids newly marked (empty when nothing
    pending cites *decision_id*, or everything that does was already
    marked).
    """
    records = read_dimension_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    already_stale = {
        str(r.get("id"))
        for r in records
        if r.get("kind") == STALE_KIND and r.get("decision_id") == decision_id
    }
    target = (
        ledger_path
        if ledger_path is not None
        else default_dimension_proposals_ledger_path(wiki_root)
    )
    newly_marked: list[str] = []
    for r in records:
        if r.get("kind") != PROPOSAL_KIND:
            continue
        proposal_id = str(r.get("id"))
        if proposal_id in resolved or proposal_id in already_stale:
            continue
        coord_origins = r.get("coord_origins") or {}
        if not isinstance(coord_origins, dict) or decision_id not in coord_origins.values():
            continue
        event = {
            "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
            "kind": STALE_KIND,
            "id": proposal_id,
            "decision_id": decision_id,
            "reason": reason,
            "created_at": now_iso(),
        }
        _append_jsonl_line(target, json.dumps(event, sort_keys=True) + "\n")
        newly_marked.append(proposal_id)
    return newly_marked


# ---------------------------------------------------------------------------
# Backfill planning (AC2 + origin-is-provenance regression)
# ---------------------------------------------------------------------------


def plan_backfill(
    missing_dimensions: tuple[str, ...], *, coord_origins: dict[str, str] | None = None
) -> dict[str, BackfillAction]:
    """Per-dimension backfill action: ``"auto"`` or ``"ask"``.

    A dimension is marked ``"auto"`` only when *coord_origins* (an honest
    ``{dimension_name: answer_id}`` mapping — see
    :func:`athenaeum.verdicts.record_pair_decision`'s ``coord_origins``
    parameter, issue athenaeum#1994) already names it: that means provenance
    for this axis was already recorded by a real human answer elsewhere, so
    replaying it costs no NEW ask. Every other dimension defaults to
    ``"ask"`` — this module never guesses a value from raw intake fields
    (e.g. a page's ``origin_scope``) on its own authority.

    **The one hard exception, enforced here regardless of what
    *coord_origins* says:** the kernel ``scope`` dimension
    (:data:`athenaeum.dimensions.SCOPE`) is ALWAYS ``"ask"``, never
    ``"auto"`` — even when *coord_origins* carries a ``"scope"`` entry. A
    page's raw ``origin_scope`` looks exactly like a free, ready-made value
    for the ``scope`` coordinate, and that resemblance is the hazard: origin
    scope is where a claim came from, not a ratified statement about what
    scope it claims. Silently treating the two as the same value is
    precisely the failure athenaeum#714's "origin is provenance, not an answer"
    rule exists to forbid, and it would reappear here as a newly-registered
    dimension quietly seeded from the wrong source the very first time it
    was backfilled. This exception is checked FIRST, before consulting
    *coord_origins* at all, so no future caller can route around it by
    passing a constructed ``coord_origins["scope"]`` entry.
    """
    origins = coord_origins or {}
    plan: dict[str, BackfillAction] = {}
    for name in missing_dimensions:
        if name == SCOPE.name:
            plan[name] = "ask"
        elif name in origins:
            plan[name] = "auto"
        else:
            plan[name] = "ask"
    return plan


def enforce_ask_budget(
    plan: dict[str, BackfillAction],
    *,
    pair_count: int,
    budget_cap: int,
) -> tuple[dict[str, BackfillAction], int, bool]:
    """Bound a drafted proposal's manual-backfill tail against *budget_cap*.

    Returns ``(final_plan, ask_count, narrowed)``. ``ask_count`` is the
    number of individual human asks the plan would cost: one ask resolves
    every ``"ask"``-marked dimension for one affected pair in a single
    coordinate answer (mirrors how
    :func:`athenaeum.decision_answers._apply_coordinate_answer` already
    answers several dimensions for one pair in one decision), so
    ``ask_count = pair_count`` whenever at least one dimension needs asking,
    and ``0`` when every dimension is already ``"auto"``.

    When the plan is within budget, it is returned unchanged with
    ``narrowed=False``. When it is over budget, two remedies are tried in
    order (AC2's own "narrower ``applies_to``, or auto-backfill only"),
    and the drafter never ships an over-budget plan:

    1. **Auto-backfill only** — every ``"ask"`` dimension OTHER than
       ``scope`` (which :func:`plan_backfill` already forbids flipping) is
       flipped to ``"auto"``. If that alone brings the remaining ask count
       within budget, it is used.
    2. **Narrower ``applies_to``** — if asking is still required (because
       ``scope`` is among the remaining ask dimensions, or flipping
       everything else still was not enough), the caller is told to narrow
       the proposal's ``applies_to`` population: the number of pairs this
       proposal actually asks about is capped at
       ``budget_cap // len(remaining_ask_dimensions)`` (floor division, so
       the resulting ask count never exceeds the cap even when it does not
       divide evenly).
    """
    ask_dims = [name for name, action in plan.items() if action == "ask"]
    if not ask_dims:
        return plan, 0, False

    ask_count = pair_count
    if ask_count <= budget_cap:
        return plan, ask_count, False

    # Remedy 1: auto-backfill only (never flips `scope` — plan_backfill's
    # exception is a plan-level invariant, re-asserted here rather than
    # trusted silently).
    narrowed_plan = dict(plan)
    for name in ask_dims:
        if name != SCOPE.name:
            narrowed_plan[name] = "auto"
    remaining_ask = [name for name, action in narrowed_plan.items() if action == "ask"]

    if not remaining_ask:
        return narrowed_plan, 0, True

    ask_count = pair_count
    if ask_count <= budget_cap:
        return narrowed_plan, ask_count, True

    # Remedy 2: narrow the affected-pair population so the remaining ask
    # dimensions' cost fits the cap exactly.
    capped_pairs = max(0, budget_cap // len(remaining_ask))
    return narrowed_plan, capped_pairs * len(remaining_ask), True


# ---------------------------------------------------------------------------
# Drafting (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionProposalDraft:
    """One drafted proposal: a candidate new dimension + its backfill plan."""

    id: str
    name: str
    kind: str
    null_semantics: str
    separates: bool
    applies_to: dict[str, Any]
    example_pairs: tuple[str, ...]
    backfill_plan: dict[str, BackfillAction]
    ask_count: int
    applies_to_narrowed: bool
    count: int
    window_days: int
    threshold: int
    #: Issue athenaeum#2017 (AC5): the honest ``{dimension_name: answer_id}``
    #: provenance mapping this draft's "auto" backfill entries were planned
    #: from (see :func:`plan_backfill`'s *coord_origins* parameter) —
    #: persisted so a later revocation of one of those answers
    #: (:func:`athenaeum.resolution_claims.revoke_resolution_claim`) can
    #: find and stale-mark this proposal via
    #: :func:`mark_proposals_stale_for_decision`. Empty for every proposal
    #: whose backfill plan is entirely ``"ask"`` (the pre-athenaeum#2017 case).
    coord_origins: dict[str, str] = field(default_factory=dict)

    def to_ledger_record(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": self.id,
            "created_at": _now_iso(now),
            "name": self.name,
            "dimension_kind": self.kind,
            "null_semantics": self.null_semantics,
            "separates": self.separates,
            "applies_to": dict(self.applies_to),
            "applies_to_narrowed": self.applies_to_narrowed,
            "example_pairs": list(self.example_pairs),
            "backfill_plan": dict(self.backfill_plan),
            "ask_count": self.ask_count,
            "count": self.count,
            "window_days": self.window_days,
            "threshold": self.threshold,
            "coord_origins": dict(self.coord_origins),
        }


def _derive_applies_to(key: ShapeKey) -> dict[str, Any]:
    """Selector narrowed to the typed cluster the shape was mined from.

    ``{frontmatter_key: allowed_value(s)}`` — the same shape
    :class:`athenaeum.dimensions.Dimension.applies_to` already uses. Built
    straight from the ``ShapeKey``'s own typed fields (never free text),
    dropping ``None`` entries (an absent coordinate on one side is not a
    selector VALUE).
    """
    applies_to: dict[str, Any] = {}
    memory_classes = sorted({v for v in key.memory_classes if v is not None})
    if memory_classes:
        applies_to["memory_class"] = memory_classes
    scopes = sorted({v for v in key.scopes if v is not None})
    if scopes:
        applies_to["scope"] = scopes
    return applies_to


def draft_dimension_proposal(
    shape: MinedShape,
    dimension_name: str,
    *,
    coord_origins: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> DimensionProposalDraft:
    """Draft one proposal for *dimension_name* out of *shape*'s missing axes.

    Deterministic — every field is derived from *shape* and the static
    defaults below; no LLM call, no client, no randomness.

    ``kind``/``null_semantics``/``separates`` ship with the same
    conservative defaults for every drafted axis:
    :data:`~athenaeum.dimensions.DimensionKind.HIERARCHY` (the same kind
    :data:`athenaeum.dimensions.SCOPE` uses — a generic "classifies into a
    bucket" axis, the safest default when the drafter has no evidence of
    the axis's true shape), :data:`~athenaeum.dimensions.NullMeans.UNKNOWN`
    (absence is not asserted as universal), and ``separates=True`` (the
    mined shape is, by construction, naming an axis the comparator could
    not resolve as a required separator — see
    :mod:`athenaeum.signal_mining`'s module docstring). A human ratifying
    this proposal (the next child) can rename or correct any of these; this
    drafter never claims certainty it does not have.
    """
    key = shape.key
    item_id = proposal_item_id(dimension_name, key)
    backfill = plan_backfill(key.missing_dimensions, coord_origins=coord_origins)
    budget_cap = resolve_decisions_budget_items_per_day_max(config)
    final_plan, ask_count, narrowed = enforce_ask_budget(
        backfill, pair_count=shape.count, budget_cap=budget_cap
    )
    applies_to = _derive_applies_to(key)
    if narrowed:
        applies_to = dict(applies_to)
        applies_to["max_pairs"] = ask_count if ask_count else 0

    return DimensionProposalDraft(
        id=item_id,
        name=dimension_name,
        kind=DimensionKind.HIERARCHY,
        null_semantics=NullMeans.UNKNOWN,
        separates=True,
        applies_to=applies_to,
        example_pairs=shape.example_pairs,
        backfill_plan=final_plan,
        ask_count=ask_count,
        coord_origins=dict(coord_origins or {}),
        applies_to_narrowed=narrowed,
        count=shape.count,
        window_days=shape.window_days,
        threshold=shape.threshold,
    )


@dataclass
class DimensionProposalRunSummary:
    """Outcome of one :func:`run_dimension_proposal_drafting` pass."""

    shapes_seen: int = 0
    candidates_seen: int = 0
    proposed: int = 0
    skipped_pending: int = 0
    skipped_suppressed: int = 0
    drafts: list[DimensionProposalDraft] = field(default_factory=list)


def run_dimension_proposal_drafting(
    shapes: list[MinedShape],
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    coord_origins_by_pair: dict[str, dict[str, str]] | None = None,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> DimensionProposalRunSummary:
    """Detect + draft (AC1/AC2): triggered shapes -> drafted, ledgered proposals.

    Only :attr:`athenaeum.signal_mining.MinedShape.triggered` shapes are
    considered. One candidate per ``(shape, missing dimension name)`` pair.
    Idempotent per candidate id: a pending or rejected (dimension, shape) is
    never re-drafted (checked before any work is done, mirroring
    :func:`athenaeum.rule_proposals.run_rule_proposal_detection`).

    *coord_origins_by_pair*, optional: ``{pair_key: {dimension_name:
    answer_id}}`` honest provenance already recorded for specific example
    pairs (issue athenaeum#1994) — threaded into :func:`plan_backfill` for
    whichever of *shape*'s ``example_pairs`` carries an entry. ``None``
    (the default, every current caller) means no provenance is known, which
    is the conservative "ask everything except what is impossible to ask"
    state this function never degrades from silently.

    *dry_run* (default ``False``): when true, drafts are computed and
    returned in the summary but NOT appended to the ledger — this is the
    "dry-run listing" half of athenaeum#719 Plan step 2. A dry run makes no
    persistence call at all.
    """
    summary = DimensionProposalRunSummary()
    wiki_root = Path(wiki_root)
    origins_by_pair = coord_origins_by_pair or {}

    triggered = [s for s in shapes if s.triggered]
    summary.shapes_seen = len(triggered)
    if not triggered:
        return summary

    records = read_dimension_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved_ids = _resolved_ids(records)
    pending_ids = {
        str(r.get("id"))
        for r in records
        if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved_ids
    }

    target = (
        ledger_path
        if ledger_path is not None
        else default_dimension_proposals_ledger_path(wiki_root)
    )

    for shape in triggered:
        for dimension_name in shape.key.missing_dimensions:
            summary.candidates_seen += 1
            item_id = proposal_item_id(dimension_name, shape.key)
            # Issue athenaeum#2016: an APPROVED id must be suppressed the SAME
            # way a REJECTED one is -- before ratification existed, nothing
            # was ever in `resolved_ids` except via reject, so checking only
            # `rejected_ids` here was unreachable-dead code, not a bug; now
            # that approve is a real ledger outcome, an id resolved by
            # approve but absent from `rejected_ids` fell through both guards
            # below and got RE-DRAFTED as a brand-new proposal for a
            # dimension already registered. Checking the full resolved set
            # (approve OR reject) closes that.
            if item_id in resolved_ids:
                summary.skipped_suppressed += 1
                continue
            if item_id in pending_ids:
                summary.skipped_pending += 1
                continue

            # Provenance known for any of this shape's example pairs, for
            # THIS dimension, is honest evidence this axis can be
            # auto-populated for those pairs — merge across examples so a
            # single answered pair's provenance is not lost just because
            # another example pair in the same shape has none.
            coord_origins: dict[str, str] = {}
            for pair in shape.example_pairs:
                pair_origins = origins_by_pair.get(pair) or {}
                if dimension_name in pair_origins:
                    coord_origins[dimension_name] = pair_origins[dimension_name]

            draft = draft_dimension_proposal(
                shape,
                dimension_name,
                coord_origins=coord_origins,
                config=config,
            )
            summary.drafts.append(draft)

            if dry_run:
                continue

            _append_jsonl_line(
                target, json.dumps(draft.to_ledger_record(now=now), sort_keys=True) + "\n"
            )
            pending_ids.add(item_id)
            summary.proposed += 1

    return summary


__all__ = [
    "DIMENSION_PROPOSALS_LEDGER_VERSION",
    "DIMENSION_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "STALE_KIND",
    "BackfillAction",
    "default_dimension_proposals_ledger_path",
    "proposal_item_id",
    "read_dimension_proposals_ledger",
    "list_pending_dimension_proposals",
    "stale_proposal_ids",
    "mark_proposals_stale_for_decision",
    "plan_backfill",
    "enforce_ask_budget",
    "DimensionProposalDraft",
    "draft_dimension_proposal",
    "DimensionProposalRunSummary",
    "run_dimension_proposal_drafting",
]
