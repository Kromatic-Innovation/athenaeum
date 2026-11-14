# SPDX-License-Identifier: Apache-2.0
"""Page recompile: the nightly pass (issue athenaeum#2547, slice S6 of the
page-recompile design, athenaeum#2525).

Slices S1-S5 built the machinery: a candidate scan and segmenter
(:mod:`athenaeum.page_recompile`), a planner
(:mod:`athenaeum.page_recompile_plan`), an atomic applier
(:mod:`athenaeum.page_recompile_apply`), an undo
(:mod:`athenaeum.page_recompile_undo`) and an operator CLI. Until this
slice, NOTHING in the nightly run acted on an oversized page -- every one
of those was reachable only by an operator naming a uid. This module is the
pass itself: it walks the candidate scan in its own order, spends a bounded
budget planning the top of it, and raises each plan as a
``page-recompile`` decision item through
:mod:`athenaeum.page_recompile_queue`.

**All policy, no mechanism.** This module plans no page itself and writes
no page at all: :func:`~athenaeum.page_recompile_plan.plan_page` does the
planning and the queue module does the raising. What lives here is the
decision about WHICH candidates to spend on and WHEN to stop -- the budget,
the modes, the deadline and the suppression checks. Modelled on
:func:`athenaeum.audit_queue.run_nightly_drain`: the librarian wiring
(:func:`athenaeum.librarian._run_page_recompile_phase`) is a dozen lines
that build a client and call this.

**Four modes** (``librarian.page_recompile.mode``, default ``queue`` --
see :func:`athenaeum.config.resolve_page_recompile_mode`):

* ``off`` -- :func:`run_nightly_page_recompile` is never reached; the
  librarian phase returns before building a client. The off switch.
* ``shadow`` -- plans are drafted and recorded, no page is written and no
  decision item is raised.
* ``queue`` (the default) -- plans are drafted AND raised for approval.
* ``auto`` -- accepted, and behaves as ``queue`` in this slice. Applying
  without an operator answer is explicitly out of scope here; the slice
  that lands the unattended applier is the one that gives ``auto`` its
  distinct behaviour.

**The budget, and why there are two page budgets.** The operator ruling on
athenaeum#2525 asks for a larger budget "at first until the over-ceiling
backlog is cleared and then 3 pages a run". That is one rule with a
corpus-dependent switch, not two knobs an operator has to remember to
change: :func:`page_budget` reads ``opening_max_pages_per_run`` while ANY
candidate is over ``librarian.oversize_hard_ceiling_chars`` and
``max_pages_per_run`` once none is. Three further ceilings bound the same
run -- ``max_input_chars_per_run``, ``max_api_calls_per_run`` and
``max_open_items`` -- and each is checked BEFORE the page's first model
call, so a page refused by any of them costs nothing.

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
:mod:`athenaeum.page_recompile_plan` and
:mod:`athenaeum.page_recompile_queue` -- all at or below its own layer, so
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
    resolve_page_recompile_max_input_chars_per_run,
    resolve_page_recompile_max_open_items,
    resolve_page_recompile_max_pages_per_run,
    resolve_page_recompile_mode,
    resolve_page_recompile_opening_max_pages_per_run,
    resolve_page_size_threshold_chars,
)
from athenaeum.page_recompile import RecompileCandidate, find_recompile_candidates
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

#: ``mode`` values under which a drafted plan is RAISED as a decision item.
#: ``auto`` is included because, in this slice, ``auto`` behaves as
#: ``queue`` (see the module docstring) -- applying without an answer is out
#: of scope here.
_QUEUEING_MODES: frozenset[str] = frozenset({"queue", "auto"})

#: ``reason`` values on :class:`PageRecompileRunSummary`. Named constants
#: because the run summary and the tests both match on them.
REASON_OK = "ok"
REASON_DEADLINE_TRIPPED = "deadline-tripped"
REASON_NO_CANDIDATES = "no-candidates"
REASON_MAX_OPEN_ITEMS = "max-open-items"


@dataclass
class PageRecompileRunSummary:
    """What one nightly page-recompile phase did (issue athenaeum#2547 AC9).

    ``planned``/``queued``/``invalid``/``skipped_budget``/``backlog`` are the
    five figures the AC requires the run summary to report; the remaining
    counters name the OTHER reasons a candidate went untouched, so a
    ``backlog`` that does not shrink is always attributable to something.
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
    #: Plans raised as ``page-recompile`` decision items (0 in ``shadow``).
    queued: int = 0
    #: Plans the planner refused or validated as invalid.
    invalid: int = 0
    #: Candidates not planned because a per-run budget was spent.
    skipped_budget: int = 0
    #: Candidates not planned because the run deadline tripped mid-phase.
    skipped_deadline: int = 0
    #: Candidates skipped because a rejected plan still pins their current
    #: content hash (:func:`~athenaeum.page_recompile_queue.rejected_page_skip_reason`).
    skipped_suppressed: int = 0
    #: Candidates not planned because ``max_open_items`` was already reached.
    skipped_open_items: int = 0
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
            "skipped_budget": self.skipped_budget,
            "skipped_deadline": self.skipped_deadline,
            "skipped_suppressed": self.skipped_suppressed,
            "skipped_open_items": self.skipped_open_items,
            "api_calls": self.api_calls,
            "input_chars": self.input_chars,
        }

    def as_profile_fields(self) -> dict[str, Any]:
        """The subset carried on the run-summary profile line (AC9's five
        required figures plus the mode and the other skip reasons)."""
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
    """Plan the top of the oversized-page backlog, bounded (issue athenaeum#2547).

    *client* is the already-built per-knob client; *usage* is the run-level
    :class:`athenaeum.models.TokenUsage`, threaded into
    :func:`~athenaeum.page_recompile_plan.plan_page` so every call this phase
    makes is attributed to the ``page_recompile`` knob and counted against
    the run's own ``max_api_calls`` ceiling. *deadline_check* is the run's
    ``RunContext.deadline_exceeded`` and *heartbeat* its
    ``RunContext.tick_heartbeat`` -- both called BETWEEN pages (AC1, AC10).

    Never raises for a per-page failure: a page the planner could not plan
    is counted ``invalid`` with its reason recorded, and the pass moves on.
    The phase's own caller wraps it as well, so a defect here can never
    change a run's exit code.
    """
    from athenaeum.models import EntityIndex
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
    if queues_items:
        open_items = len(
            list_pending_page_recompile_plans(wiki_root, ledger_path=ledger_path)
        )
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

        body = _read_body(candidate.path)
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
    if summary.planned or summary.queued or summary.invalid or summary.skipped_budget:
        log.info(
            "page-recompile: mode=%s backlog=%d budget=%d%s planned=%d queued=%d "
            "invalid=%d skipped_budget=%d skipped_suppressed=%d skipped_deadline=%d "
            "calls=%d",
            mode,
            summary.backlog,
            budget,
            " (opening)" if opening else "",
            summary.planned,
            summary.queued,
            summary.invalid,
            summary.skipped_budget,
            summary.skipped_suppressed,
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


def _read_body(path: Path) -> str | None:
    """The page's BODY (frontmatter stripped), or ``None`` if unreadable."""
    from athenaeum.models import parse_frontmatter

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    _meta, body = parse_frontmatter(text)
    return body


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
