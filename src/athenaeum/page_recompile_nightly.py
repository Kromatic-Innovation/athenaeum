# SPDX-License-Identifier: Apache-2.0
"""Page recompile: the nightly pass (issue athenaeum#2547, slice S6, and
issue athenaeum#2549, slice S8, of the page-recompile design, athenaeum#2525).

Slices S1-S5 built the machinery: a candidate scan and segmenter
(:mod:`athenaeum.page_recompile`), a planner
(:mod:`athenaeum.page_recompile_plan`), an atomic applier
(:mod:`athenaeum.page_recompile_apply`), an undo
(:mod:`athenaeum.page_recompile_undo`) and an operator CLI. S6 (athenaeum#2547)
made this module the first thing in the nightly run to act on an oversized
page at all -- walking the candidate scan in its own order, spending a
bounded budget planning the top of it, and raising each plan as a
``page-recompile`` decision item through
:mod:`athenaeum.page_recompile_queue`. S8 (athenaeum#2549, this slice) turns
the switch the whole design was building toward: in the default mode, a
``structure-only`` plan that passes every validation is applied right here,
unattended, with no decision item at all.

**All policy, no mechanism.** This module plans no page itself:
:func:`~athenaeum.page_recompile_plan.plan_page` does the planning, the
queue module does the raising, and :func:`~athenaeum.page_recompile_apply.write_apply`
does the one write this module makes on an operator's behalf. What lives
here is the decision about WHICH candidates to spend on, WHEN to stop, and
-- as of this slice -- WHICH drafted plans may be applied without being
asked. Modelled on :func:`athenaeum.audit_queue.run_nightly_drain`: the
librarian wiring (:func:`athenaeum.librarian._run_page_recompile_phase`) is
a dozen lines that build a client and call this.

**Four modes** (``librarian.page_recompile.mode``, default ``auto`` --
see :func:`athenaeum.config.resolve_page_recompile_mode`):

* ``off`` -- :func:`run_nightly_page_recompile` is never reached; the
  librarian phase returns before building a client. The off switch.
* ``shadow`` -- plans are drafted and recorded, no page is written and no
  decision item is raised.
* ``queue`` -- plans are drafted AND raised for approval.
* ``auto`` (the default) -- drafts a plan for every candidate the budget
  reaches, same as ``queue``, but a plan that is BOTH valid AND
  :data:`~athenaeum.page_recompile_plan.SCOPE_STRUCTURE_ONLY` is applied
  right here instead of queued, when
  :func:`athenaeum.config.resolve_page_recompile_auto_apply_class` resolves
  to ``structure-only`` (also the default) AND the page's own ``type:``
  is not in :func:`athenaeum.config.resolve_page_recompile_never_auto_types`.
  Every other plan in ``auto`` -- ``cross-page``, over the
  ``max_auto_applies_per_run`` bound this run, a ``never_auto_types`` page,
  or ``auto_apply_class: none`` -- is queued exactly as in ``queue``. See
  "How a structure-only plan gets authorised" below.

**How a structure-only plan gets authorised, without touching the
athenaeum#2548 approval interlock.** :func:`~athenaeum.page_recompile_apply.write_apply`
already has, from athenaeum#2548, an UNCONDITIONAL carve-out: a plan whose
own :attr:`~athenaeum.page_recompile_plan.RecompilePlan.scope` is
``structure-only`` needs no *verdict* at all -- only a ``cross-page`` plan
is gated on ``_cross_page_approval_refusal``'s two-part check (an approving
verdict AND a ledger record proving it was actually raised). This module
never passes a verdict, forged or otherwise, and never calls
``write_apply`` for a plan it has not itself confirmed is
``structure-only`` (read off the SAME :attr:`RecompilePlan.scope` the
planner already set via
:func:`~athenaeum.page_recompile_plan.classify_plan_scope` -- never
re-derived here). A ``cross-page`` plan from this phase is routed to the
UNCHANGED queue path, exactly as before, so the interlock is exercised for
it exactly as it always was: this module adds a gate on TOP of the
existing authorisation (deciding whether to call the already-safe
``write_apply`` path at all), not a new way INTO it.

**The budget, and why there are two page budgets.** The operator ruling on
athenaeum#2525 asks for a larger budget "at first until the over-ceiling
backlog is cleared and then 3 pages a run". That is one rule with a
corpus-dependent switch, not two knobs an operator has to remember to
change: :func:`page_budget` reads ``opening_max_pages_per_run`` while ANY
candidate is over ``librarian.oversize_hard_ceiling_chars`` and
``max_pages_per_run`` once none is. Three further ceilings bound the same
run -- ``max_input_chars_per_run``, ``max_api_calls_per_run`` and
``max_open_items`` -- and each is checked BEFORE the page's first model
call, so a page refused by any of them costs nothing. A FOURTH budget,
``max_auto_applies_per_run`` (athenaeum#2549 AC5), bounds only the APPLY
step: a structure-only plan found after the bound is already spent this run
is neither applied nor queued -- it simply waits for a later run, when the
bound resets.

**No reserved share of the run deadline**, exactly as
:func:`athenaeum.librarian._run_rule_proposal_phase`'s docstring argues for
itself and as the operator ruling on athenaeum#2525 states outright: this
phase runs last, after the entity-tier and auto-memory phases have already
spent whatever ``run_deadline`` allowed, so carving a FRESH share here
would extend the run past ``max_runtime``. It participates in the run's own
deadline instead: skipped entirely when already expired, and re-checked
between pages.

Layering: L4 domain/pipeline, the same layer as every other
``page_recompile*`` module and as :mod:`athenaeum.audit_queue`, the
nightly-phase body it is modelled on. Imports :mod:`athenaeum.config`
(L2) and its L4 siblings :mod:`athenaeum.page_recompile`,
:mod:`athenaeum.page_recompile_plan`, :mod:`athenaeum.page_recompile_queue`
and (function-local, inside :func:`run_nightly_page_recompile`, same as the
``plan_page``/``EntityIndex`` imports already there)
:mod:`athenaeum.page_recompile_apply` -- all at or below its own layer, so
none of these edges needs an ``ALLOWED_UPWARD_EDGES`` entry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.config import (
    resolve_oversize_hard_ceiling_chars,
    resolve_page_recompile_auto_apply_class,
    resolve_page_recompile_max_api_calls_per_run,
    resolve_page_recompile_max_auto_applies_per_run,
    resolve_page_recompile_max_input_chars_per_run,
    resolve_page_recompile_max_open_items,
    resolve_page_recompile_max_pages_per_run,
    resolve_page_recompile_mode,
    resolve_page_recompile_never_auto_types,
    resolve_page_recompile_opening_max_pages_per_run,
    resolve_page_size_threshold_chars,
)
from athenaeum.page_recompile import RecompileCandidate, find_recompile_candidates
from athenaeum.page_recompile_plan import (
    SCOPE_STRUCTURE_ONLY,
    invalid_plan_skip_reason,
    page_content_hash,
)
from athenaeum.page_recompile_queue import (
    build_plan_outline,
    list_pending_page_recompile_plans,
    queue_plan_for_approval,
    rejected_page_skip_reason,
)

log = logging.getLogger(__name__)

#: The knob every model call this phase makes is attributed to in the spend
#: ledger. The SAME knob :mod:`athenaeum.page_recompile_plan`'s planning
#: call already uses (its ``PLAN_KNOB``), named here so the librarian wiring
#: records the phase's provider/model under it without importing the planner
#: for a string.
PAGE_RECOMPILE_KNOB = "page_recompile"

#: ``mode`` values under which the phase plans at all.
_PLANNING_MODES: frozenset[str] = frozenset({"shadow", "queue", "auto"})

#: ``mode`` values under which a drafted plan MAY be raised as a decision
#: item. ``auto`` is included because it still queues every plan that is
#: NOT auto-applied -- ``cross-page``, over the ``max_auto_applies_per_run``
#: bound, a ``never_auto_types`` page, or ``auto_apply_class: none``. A plan
#: that IS auto-applied is routed to ``write_apply`` instead and never
#: reaches this path at all (see :func:`_eligible_for_auto_apply`).
_QUEUEING_MODES: frozenset[str] = frozenset({"queue", "auto"})

#: ``reason`` values on :class:`PageRecompileRunSummary`. Named constants
#: because the run summary and the tests both match on them.
REASON_OK = "ok"
REASON_DEADLINE_TRIPPED = "deadline-tripped"
REASON_NO_CANDIDATES = "no-candidates"
REASON_MAX_OPEN_ITEMS = "max-open-items"


@dataclass
class PageRecompileRunSummary:
    """What one nightly page-recompile phase did (issues athenaeum#2547 AC9,
    athenaeum#2549 AC6).

    ``planned``/``queued``/``invalid``/``skipped_budget``/``backlog`` are the
    five figures athenaeum#2547 AC9 requires; ``applied``/``applied_plans``
    are the ones athenaeum#2549 AC6 adds. The remaining counters name the
    OTHER reasons a candidate went untouched, so a ``backlog`` that does not
    shrink is always attributable to something.
    """

    reason: str
    mode: str
    auto_apply_class: str
    #: Candidates the scan found this run -- the denominator for everything
    #: below.
    backlog: int = 0
    #: Candidates over ``librarian.oversize_hard_ceiling_chars``; nonzero
    #: means the OPENING page budget applied.
    over_ceiling: int = 0
    #: The page budget actually in force, and whether it was the opening one.
    page_budget: int = 0
    opening_budget: bool = False
    #: Plans drafted that came back valid.
    planned: int = 0
    #: Plans raised as ``page-recompile`` decision items (0 in ``shadow``,
    #: and 0 for a plan that was auto-applied instead -- see ``applied``).
    queued: int = 0
    #: Plans the planner refused or validated as invalid.
    invalid: int = 0
    #: Issue athenaeum#2549 AC2/AC6: structure-only plans applied unattended
    #: this run, in mode ``auto`` with ``auto_apply_class: structure-only``.
    applied: int = 0
    #: Issue athenaeum#2549 AC6: one entry per :attr:`applied` plan -- its
    #: plan id and the counts of sub-pages created and sections merged, so
    #: an auto-applied plan is nameable from the run summary alone, the same
    #: way a queued one is nameable from the decision item it raised.
    applied_plans: list[dict[str, Any]] = field(default_factory=list)
    #: Candidates not planned because a per-run budget was spent.
    skipped_budget: int = 0
    #: Candidates not planned because the run deadline tripped mid-phase.
    skipped_deadline: int = 0
    #: Candidates skipped because a rejected plan still pins their current
    #: content hash (:func:`~athenaeum.page_recompile_queue.rejected_page_skip_reason`).
    skipped_suppressed: int = 0
    #: Issue athenaeum#2549 AC4: candidates not planned because the LAST
    #: plan drafted for their current content hash was invalid
    #: (:func:`~athenaeum.page_recompile_plan.invalid_plan_skip_reason`) --
    #: a page skipped this way is counted here, not in ``invalid`` again,
    #: on a night it was not actually re-planned.
    skipped_invalid: int = 0
    #: Candidates not planned because ``max_open_items`` was already reached.
    skipped_open_items: int = 0
    #: Candidates skipped because THIS page already has an unanswered plan
    #: pinned to its current content. Distinct from
    #: :attr:`skipped_open_items`, which is the queue-wide cap: this one is
    #: per page, and without it a handful of unanswered pages would consume
    #: the whole page budget every night and the rest of the backlog would
    #: never advance.
    skipped_already_queued: int = 0
    #: Issue athenaeum#2549 AC5: a valid structure-only plan that would
    #: otherwise auto-apply, but found ``max_auto_applies_per_run`` already
    #: spent this run. Neither applied nor queued -- it waits for a later
    #: run, when the bound resets.
    skipped_auto_apply_budget: int = 0
    #: Model calls this phase made, measured off the run-level usage counter.
    api_calls: int = 0
    #: Page-body characters planned over.
    input_chars: int = 0
    #: Per-page errors the planner reported, for the log line only.
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "mode": self.mode,
            "auto_apply_class": self.auto_apply_class,
            "backlog": self.backlog,
            "over_ceiling": self.over_ceiling,
            "page_budget": self.page_budget,
            "opening_budget": self.opening_budget,
            "planned": self.planned,
            "queued": self.queued,
            "invalid": self.invalid,
            "applied": self.applied,
            "applied_plans": list(self.applied_plans),
            "skipped_budget": self.skipped_budget,
            "skipped_deadline": self.skipped_deadline,
            "skipped_suppressed": self.skipped_suppressed,
            "skipped_invalid": self.skipped_invalid,
            "skipped_open_items": self.skipped_open_items,
            "skipped_already_queued": self.skipped_already_queued,
            "skipped_auto_apply_budget": self.skipped_auto_apply_budget,
            "api_calls": self.api_calls,
            "input_chars": self.input_chars,
        }

    def as_profile_fields(self) -> dict[str, Any]:
        """The subset carried on the run-summary profile line (athenaeum#2547
        AC9's five required figures, athenaeum#2549 AC6's ``applied``/
        ``applied_plans``, plus the mode and the other skip reasons)."""
        fields = self.to_dict()
        fields.pop("auto_apply_class", None)
        return fields


def page_budget(
    candidates: "Sequence[RecompileCandidate]", config: dict[str, Any] | None
) -> tuple[int, bool]:
    """``(budget, is_opening)`` -- the per-run page budget in force (AC4/AC5).

    The OPENING budget (``opening_max_pages_per_run``) applies while ANY
    candidate in *candidates* is over the hard ceiling; the steady budget
    (``max_pages_per_run``) applies once none is. The switch is read off the
    corpus every run, so a corpus that clears its over-ceiling backlog drops
    to the steady budget on the very next run with no config change -- which
    is what the operator ruling on athenaeum#2525 asks for.
    """
    opening = any(candidate.over_ceiling for candidate in candidates)
    if opening:
        return resolve_page_recompile_opening_max_pages_per_run(config), True
    return resolve_page_recompile_max_pages_per_run(config), False


def run_nightly_page_recompile(
    wiki_root: Path,
    *,
    client: Any,
    config: dict[str, Any] | None = None,
    now: date | None = None,
    usage: Any = None,
    deadline_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
    dry_run: bool = False,
    ledger_path: Path | None = None,
) -> PageRecompileRunSummary:
    """Plan the top of the oversized-page backlog, bounded (issue
    athenaeum#2547), and -- in mode ``auto`` -- apply a valid structure-only
    plan unattended (issue athenaeum#2549).

    *client* is the already-built per-knob client; *usage* is the run-level
    :class:`athenaeum.models.TokenUsage`, threaded into
    :func:`~athenaeum.page_recompile_plan.plan_page` (and, for an
    auto-applied plan's own merge-verify call, into
    :func:`~athenaeum.page_recompile_apply.write_apply`) so every call this
    phase makes is attributed to the knob that made it and counted against
    the run's own ``max_api_calls`` ceiling. *deadline_check* is the run's
    ``RunContext.deadline_exceeded`` and *heartbeat* its
    ``RunContext.tick_heartbeat`` -- both called BETWEEN pages (AC1, AC10).

    Never raises for a per-page failure: a page the planner could not plan
    is counted ``invalid`` with its reason recorded, and the pass moves on.
    The phase's own caller wraps it as well, so a defect here can never
    change a run's exit code.
    """
    from athenaeum.models import EntityIndex, resolve_page_type
    from athenaeum.page_recompile_apply import write_apply
    from athenaeum.page_recompile_plan import plan_page

    mode = resolve_page_recompile_mode(config)
    auto_apply_class = resolve_page_recompile_auto_apply_class(config)
    summary = PageRecompileRunSummary(
        reason=REASON_OK, mode=mode, auto_apply_class=auto_apply_class
    )
    if mode not in _PLANNING_MODES:
        # Unreachable through the librarian wiring, which gates on `off`
        # before building a client -- kept so a direct caller gets the same
        # contract rather than planning under a mode that forbids it.
        return summary

    scan = find_recompile_candidates(wiki_root, config)
    summary.backlog = len(scan.candidates)
    summary.over_ceiling = sum(1 for c in scan.candidates if c.over_ceiling)
    budget, opening = page_budget(scan.candidates, config)
    summary.page_budget = budget
    summary.opening_budget = opening
    if not scan.candidates:
        summary.reason = REASON_NO_CANDIDATES
        return summary

    queues_items = mode in _QUEUEING_MODES
    # uid -> the content hash an unanswered plan for that page is pinned to.
    # Read ONCE, before the loop: it cannot change under us (this phase runs
    # under the run lock and is the only writer of queued items here), and
    # re-reading the ledger per candidate would be a quadratic read over a
    # file that grows with every plan ever drafted.
    open_plan_hashes: dict[str, str] = {}
    if queues_items:
        pending = list_pending_page_recompile_plans(wiki_root, ledger_path=ledger_path)
        for record in pending:
            uid = str(record.get("uid") or "")
            pinned = record.get("content_hash")
            if uid and isinstance(pinned, str) and pinned:
                open_plan_hashes[uid] = pinned
        open_items = len(pending)
        max_open = resolve_page_recompile_max_open_items(config)
        if open_items >= max_open:
            # No model call at all: drafting a plan that cannot be raised is
            # spend with no outcome (AC4).
            summary.reason = REASON_MAX_OPEN_ITEMS
            summary.skipped_open_items = summary.backlog
            log.info(
                "page-recompile: %d unanswered item(s) at the max_open_items cap "
                "of %d; %d candidate(s) left unplanned this run",
                open_items,
                max_open,
                summary.backlog,
            )
            return summary

    max_input_chars = resolve_page_recompile_max_input_chars_per_run(config)
    max_api_calls = resolve_page_recompile_max_api_calls_per_run(config)
    threshold = resolve_page_size_threshold_chars(config)
    ceiling = resolve_oversize_hard_ceiling_chars(config)
    # Issue athenaeum#2549 AC5/AC8: only consulted in mode `auto`, but
    # resolved unconditionally -- both are cheap, and the alternative is a
    # second `mode == "auto"` branch above this one for no real saving.
    max_auto_applies = resolve_page_recompile_max_auto_applies_per_run(config)
    never_auto_types = resolve_page_recompile_never_auto_types(config)
    index = EntityIndex(wiki_root)
    calls_at_start = _api_calls(usage)

    for position, candidate in enumerate(scan.candidates):
        # AC1: the deadline is checked BETWEEN pages, and AC10: the run-lock
        # heartbeat is refreshed there too, so a long pass is never mistaken
        # for a wedged one.
        if heartbeat is not None:
            heartbeat()
        if deadline_check is not None and deadline_check():
            summary.skipped_deadline = summary.backlog - position
            summary.reason = REASON_DEADLINE_TRIPPED
            log.info(
                "page-recompile: run deadline tripped after %d page(s); %d "
                "candidate(s) deferred to the next run",
                position,
                summary.skipped_deadline,
            )
            break

        if summary.planned + summary.invalid >= budget:
            summary.skipped_budget += 1
            continue
        if summary.input_chars + candidate.chars > max_input_chars:
            summary.skipped_budget += 1
            continue
        if _api_calls(usage) - calls_at_start >= max_api_calls:
            summary.skipped_budget += 1
            continue

        page_meta, body = _read_meta_and_body(candidate.path)
        if body is None:
            summary.invalid += 1
            summary.errors.append(f"{candidate.uid}: page unreadable")
            continue
        suppressed = rejected_page_skip_reason(
            wiki_root, uid=candidate.uid, body=body, ledger_path=ledger_path
        )
        if suppressed:
            summary.skipped_suppressed += 1
            continue
        # Issue athenaeum#2549 AC4: the LAST plan drafted for this page's
        # current content hash was invalid -- re-planning identical bytes
        # would spend a full labelling-plus-plan(-plus-verify) call to
        # arrive at the same invalid plan again. Checked for every planning
        # mode, not only `auto`: the spend this avoids is the planner's,
        # which every mode in `_PLANNING_MODES` pays.
        invalid_reason = invalid_plan_skip_reason(
            wiki_root, uid=candidate.uid, body=body, ledger_path=ledger_path
        )
        if invalid_reason:
            summary.skipped_invalid += 1
            continue
        # This page already has an unanswered plan pinned to exactly these
        # bytes, so re-planning it would spend a plan and a verify call to
        # arrive at the identical plan id -- and would burn a slot of the
        # page budget that a page further down the backlog needs. Checked
        # BEFORE any model call, so an unanswered page costs nothing on
        # every subsequent night.
        #
        # A CHANGED page is deliberately re-planned rather than skipped: its
        # new content hash makes a new plan, and the old item -- pinned to
        # bytes the page no longer holds -- is one no answer could have
        # applied anyway (it resolves as `stale` when answered).
        if open_plan_hashes.get(candidate.uid) == page_content_hash(body):
            summary.skipped_already_queued += 1
            continue

        summary.input_chars += candidate.chars
        try:
            result = plan_page(
                wiki_root=wiki_root,
                uid=candidate.uid,
                client=client,
                config=config,
                index=index,
                ledger_path=ledger_path,
                record=not dry_run,
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - one bad page must never
            # take down the nightly run; it is counted and named instead.
            log.warning(
                "page-recompile: planning %s failed (%s): %s",
                candidate.uid,
                type(exc).__name__,
                exc,
            )
            summary.invalid += 1
            summary.errors.append(f"{candidate.uid}: {type(exc).__name__}")
            continue

        plan = result.plan
        if plan is None or not plan.valid:
            summary.invalid += 1
            if result.error:
                summary.errors.append(f"{candidate.uid}: {result.error}")
            elif plan is not None:
                summary.errors.append(
                    f"{candidate.uid}: "
                    + ", ".join(sorted({issue.code for issue in plan.issues}))
                )
            continue

        summary.planned += 1

        # Issue athenaeum#2549 AC2/AC3/AC5/AC8: decide whether THIS plan may
        # be applied unattended, reading the plan's own scope rather than
        # re-deriving it -- `plan.scope` is already
        # `classify_plan_scope`'s answer, set by the planner. A plan that is
        # NOT eligible here (wrong mode/class, cross-page, a never_auto_types
        # page) falls straight through to the unchanged queue path below, so
        # a cross-page plan is queued exactly as it always was.
        eligible_for_auto = (
            mode == "auto"
            and auto_apply_class == "structure-only"
            and plan.scope == SCOPE_STRUCTURE_ONLY
            and resolve_page_type(page_meta) not in never_auto_types
            and not dry_run
        )
        if eligible_for_auto:
            if summary.applied >= max_auto_applies:
                # Neither applied nor queued (AC5): a structure-only plan
                # under `auto_apply_class: structure-only` is never a
                # decision item, budget-exhausted or not -- it simply waits
                # for a run where `max_auto_applies_per_run` has not yet
                # been spent.
                summary.skipped_auto_apply_budget += 1
                continue
            outcome = write_apply(
                wiki_root,
                plan_id=result.plan_id,
                # The SAME client already built for the `page_recompile`
                # knob, reused for the merge-verify call `write_apply` may
                # make -- exactly how `plan_page` already reuses it for
                # its OWN internal verify step (see this module's
                # docstring). No *verdict* is ever passed: a structure-only
                # plan does not need one (`write_apply` only consults
                # *verdict* for a cross-page plan), so this call forges
                # nothing and the athenaeum#2548 approval interlock is
                # never touched.
                client=client,
                config=config,
                ledger_path=ledger_path,
                now=now,
            )
            if outcome.ok:
                summary.applied += 1
                summary.applied_plans.append(
                    {
                        "plan_id": outcome.plan_id,
                        "uid": outcome.uid,
                        "sub_pages_created": len(outcome.created_uids),
                        "sections_merged": len(plan.merges),
                    }
                )
            else:
                # The plan is already recorded (as `planned`); a write-time
                # refusal (a race on the content hash, a post-validation
                # issue `compute_apply` catches that the planner's own
                # checks did not) is reported and the run moves on, the
                # same "already counted, do not also lose the run" rule the
                # queueing failure just below follows.
                log.warning(
                    "page-recompile: auto-apply of plan %s failed: %s",
                    result.plan_id,
                    outcome.reason,
                )
                summary.errors.append(f"{candidate.uid}: auto-apply refused: {outcome.reason}")
            continue

        if not queues_items or dry_run:
            continue
        try:
            outline = build_plan_outline(
                plan,
                _units_for(body),
                page_name=candidate.path.stem,
                page_chars=candidate.chars,
            )
            queue_plan_for_approval(
                wiki_root,
                plan_id=result.plan_id,
                plan=plan,
                outline=outline,
                ledger_path=ledger_path,
            )
        except Exception as exc:  # noqa: BLE001 - the plan is already
            # recorded; failing to RAISE it must not lose the run.
            log.warning(
                "page-recompile: queueing plan %s failed (%s): %s",
                result.plan_id,
                type(exc).__name__,
                exc,
            )
            summary.errors.append(f"{candidate.uid}: queue failed")
            continue
        summary.queued += 1

    summary.api_calls = max(0, _api_calls(usage) - calls_at_start)
    if (
        summary.planned
        or summary.queued
        or summary.invalid
        or summary.applied
        or summary.skipped_budget
        or summary.skipped_already_queued
    ):
        log.info(
            "page-recompile: mode=%s backlog=%d budget=%d%s planned=%d queued=%d "
            "applied=%d invalid=%d skipped_budget=%d skipped_suppressed=%d "
            "skipped_invalid=%d skipped_already_queued=%d "
            "skipped_auto_apply_budget=%d skipped_deadline=%d calls=%d",
            mode,
            summary.backlog,
            budget,
            " (opening)" if opening else "",
            summary.planned,
            summary.queued,
            summary.applied,
            summary.invalid,
            summary.skipped_budget,
            summary.skipped_suppressed,
            summary.skipped_invalid,
            summary.skipped_already_queued,
            summary.skipped_auto_apply_budget,
            summary.skipped_deadline,
            summary.api_calls,
        )
    # Resolved threshold/ceiling are logged once at DEBUG so an operator
    # reading a surprising backlog can see which figures produced it (AC6
    # names `page_size_threshold_chars` as THE trigger setting).
    log.debug(
        "page-recompile: threshold=%d chars ceiling=%d chars auto_apply_class=%s",
        threshold,
        ceiling,
        auto_apply_class,
    )
    return summary


def _api_calls(usage: Any) -> int:
    """``usage.api_calls``, or 0 for a caller that passed none."""
    if usage is None:
        return 0
    try:
        return int(getattr(usage, "api_calls", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _read_meta_and_body(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """*path*'s frontmatter mapping and BODY (frontmatter stripped), or
    ``(None, None)`` if unreadable.

    The meta half is issue athenaeum#2549 AC8's own read: the candidate's
    ``type:`` (:func:`athenaeum.models.resolve_page_type`) is what
    ``never_auto_types`` is checked against, and re-reading the file a
    second time just for that would double the per-candidate I/O this
    phase already does once here.
    """
    from athenaeum.models import parse_frontmatter

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    meta, body = parse_frontmatter(text)
    return (meta if isinstance(meta, dict) else {}), body


def _units_for(body: str) -> tuple[Any, ...]:
    """The segmenter's units for *body* -- the outline's only source of unit
    sizes and excerpts. Re-segmenting is free of a model call and is exactly
    what the planner itself did, so the ids line up."""
    from athenaeum.page_recompile import segment_page

    return tuple(segment_page(body).units)


__all__ = [
    "PAGE_RECOMPILE_KNOB",
    "REASON_DEADLINE_TRIPPED",
    "REASON_MAX_OPEN_ITEMS",
    "REASON_NO_CANDIDATES",
    "REASON_OK",
    "PageRecompileRunSummary",
    "page_budget",
    "run_nightly_page_recompile",
]
