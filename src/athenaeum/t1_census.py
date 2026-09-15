# SPDX-License-Identifier: Apache-2.0
"""Run-scoped census of T1-screened vs. unscreened merge-proposal writes (issue athenaeum#1620).

athenaeum#1620 found the T1 reasoning-tier screen reaches roughly 7% of live
merge-proposal inflow: two of the three writers of ``_pending_merges.md``
(:mod:`athenaeum.name_collisions`, :mod:`athenaeum.name_structure`) never
call it at all, and even the one writer that does
(:mod:`athenaeum.merge`, via :func:`athenaeum.reasoning_screens.
t1_screen_rejects_merge_proposal`) turned out to be skipping the screen
silently on every proposal in the measured window. This module is the
counter that makes that split OBSERVABLE per run instead of requiring a
by-hand rationale-text audit the next time it drifts (AC3).

Layering: L0 (stdlib-only leaf/primitive) — a plain dataclass counter with
no imports from elsewhere in :mod:`athenaeum`, so any layer may depend on
it. Three call sites reach it directly, all at or above L4
(:mod:`athenaeum.reasoning_screens`, :mod:`athenaeum.name_collisions`,
:mod:`athenaeum.name_structure`), plus :mod:`athenaeum.librarian` (L5) to
reset it at the top of every run and read it back into the run summary.

Global mutable state, deliberately. The alternative — threading a new
out-param through :func:`athenaeum.merge.merge_clusters_to_wiki` and every
one of its callers, PLUS separate out-params on
:func:`athenaeum.name_collisions.resolve_name_collisions` and
:func:`athenaeum.name_structure.propose_qualified_name_merges` — would
touch call sites with no other reason to change, for a value that is pure
observability and never influences a decision. The existing
``out_stats: dict | None`` out-param convention
(:func:`athenaeum.merge.merge_clusters_to_wiki`'s own docstring) is exactly
this shape already, but it is populated from a single stack frame; T1's
skip reason is decided three call frames deep inside
:func:`~athenaeum.reasoning_screens.t1_screen_rejects_merge_proposal`,
which must keep its ``bool`` return contract byte-identical (see that
function's docstring) — it has no slot to also return a reason code
outward. A process-global counter sidesteps both problems, on the SAME
precedent :mod:`athenaeum.models` already sets for
``_ACTIVE_MODEL_RATES_USD_PER_MTOK`` (issue athenaeum#783): resettable via one
explicit function, and reset at the top of every run
(:func:`athenaeum.librarian.run`, next to that function's own
``new_run_id()`` call) so a long-lived process performing several runs
never leaks one run's counts into the next. ``tests/conftest.py`` resets it
after every test the same way ``_reset_model_rates`` resets the pricing
table, so no test can leak into the next either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: T1 was disabled for this run (``reasoning_tier_auditing_enabled`` resolved
#: ``False``) — the surviving reason in athenaeum#1620's own elimination analysis
#: for why the cluster path's ~30 proposals produced no T1 call.
T1_SKIP_DISABLED = "disabled"

#: No live LLM client was available for the ``reasoning_t1`` knob.
T1_SKIP_NO_CLIENT = "no_client"

#: This call is part of a dry run — never mutates ``_pending_merges.md``,
#: so the screen has nothing real to gate.
T1_SKIP_DRY_RUN = "dry_run"

#: The candidate proposal carried no member paths (nothing to screen).
T1_SKIP_NO_MEMBERS = "no_members"

#: The run-level spend ceiling had already tripped (issue athenaeum#568) — degrades
#: to an unscreened write rather than block the merge queue.
T1_SKIP_CEILING = "ceiling"

#: :mod:`athenaeum.name_collisions` writes every proposal unscreened, by
#: design (see the code comment at its own ``write_pending_merge`` call
#: site) — a deterministic name-identity match has no LLM call anywhere in
#: its chain, so there is nothing for T1 to add.
T1_UNSCREENED_NAME_COLLISION = "deliberate-name-collision"

#: :mod:`athenaeum.name_structure` writes every proposal unscreened, by
#: design (see the code comment at its own ``write_pending_merge`` call
#: site) — the scan exists precisely to escalate an ambiguity T1 could only
#: ever drop, never resolve.
T1_UNSCREENED_NAME_STRUCTURE = "deliberate-name-structure"


@dataclass
class T1Census:
    """Accumulates one run's T1-screened vs. unscreened merge-proposal writes.

    ``screened`` counts every call where the T1 tier chain actually ran
    (:func:`~athenaeum.reasoning_screens.t1_screen_rejects_merge_proposal`
    reached :func:`athenaeum.reasoning_tiers.run_reasoning_pipeline`) —
    regardless of whether the verdict was a reject or a pass-up; both are
    the screen doing its job. ``unscreened_by_reason`` counts every merge
    proposal written to ``_pending_merges.md`` WITHOUT the screen having
    run, keyed by why.
    """

    screened: int = 0
    unscreened_by_reason: dict[str, int] = field(default_factory=dict)

    def record_screened(self) -> None:
        self.screened += 1

    def record_unscreened(self, reason: str) -> None:
        self.unscreened_by_reason[reason] = self.unscreened_by_reason.get(reason, 0) + 1

    def reset(self) -> None:
        self.screened = 0
        self.unscreened_by_reason.clear()

    @property
    def unscreened(self) -> int:
        return sum(self.unscreened_by_reason.values())

    def as_profile_fields(self) -> dict[str, Any]:
        """Render into the ``fields`` shape a ``ctx.run_profile`` phase entry
        expects — see :func:`athenaeum.librarian._render_run_summary` and
        :func:`athenaeum.run_summary_log.build_run_summary_ledger_record`,
        which both consume ``run_profile`` uniformly, so this flows into the
        greppable prose line AND the durable JSONL ledger record with no
        further wiring (issue athenaeum#1620 AC3).
        """
        fields: dict[str, Any] = {"screened": self.screened, "unscreened": self.unscreened}
        if self.unscreened_by_reason:
            fields["unscreened_reasons"] = ",".join(
                f"{reason}:{count}"
                for reason, count in sorted(self.unscreened_by_reason.items())
            )
        fields["reason"] = "completed"
        return fields


#: Process-global, run-scoped singleton (see the module docstring's "Global
#: mutable state, deliberately" section for why this shape). Reset at the
#: top of every :func:`athenaeum.librarian.run` call and after every test
#: (``tests/conftest.py``'s ``_reset_t1_census`` fixture).
_CENSUS = T1Census()


def get_t1_census() -> T1Census:
    """Return the process-global run-scoped T1 census."""
    return _CENSUS


def reset_t1_census() -> None:
    """Reset the process-global T1 census. Call at the start of every run."""
    _CENSUS.reset()
