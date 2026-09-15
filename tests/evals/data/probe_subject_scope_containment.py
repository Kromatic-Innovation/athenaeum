# SPDX-License-Identifier: Apache-2.0
"""Probe `claimed_scope`-containment rule (issue athenaeum#1664, Plan step 2).

**Why this exists.** athenaeum#1664 asks whether the 2026-09-15 shadow-parity run's
0/2 on `merge`-class clusters (`measurements/shadow-parity-2026-09-15.md`) is a
*fixture artifact* -- the committed annotation rule
(`scripts/annotate_shadow_parity_subject_scope.py`) sets `claimed_scope` equal
to the shared name stem on BOTH cluster members, so `scope` always reads
EQUAL and `src/athenaeum/comparator.py`'s `_strict_containment` branch can
never fire -- or a *comparator defect*. Telling those apart needs a SECOND,
independent `claimed_scope` derivation that actually encodes containment, so
Gate 1 has something to consult. This module is that second rule. It is
NEVER applied to the committed `tests/evals/data/{detector,resolver}/cases.subject-scope.yaml`
fixtures (athenaeum#1664's Out-of-scope explicitly forbids editing them, and
athenaeum#1508's AC guarantees no coordinate there was hand-tuned after a
verdict was seen) -- it is read-only test-data logic, applied at TEST TIME
to a throwaway copy of the two sides' frontmatter dicts.

**The rule (label-blind, fixed before any verdict from applying it was
inspected -- see the module docstring's own methodological point below):**

Every cluster in the two corpora has exactly two members. Call their
existing (committed) `claimed_scope` values `cs_a`/`cs_b`. Two cases:

1. `cs_a != cs_b` (the ONE cluster with no shared name stem --
   `meeting_cadence_different_scenarios`, per
   `scripts/annotate_shadow_parity_subject_scope.py`'s `shared_stem`: an
   empty stem leaves each member's OWN name as its `claimed_scope`, so the
   two values already differ). There is no stem to nest a scope under, so
   the probe rule makes NO change -- both members keep their existing,
   non-containing `claimed_scope`, exactly preserving that pair's
   pre-probe Gate-1 behaviour.
2. `cs_a == cs_b == stem` (every other cluster). Pick the WINNER -- the
   member that keeps the bare `stem` as its probe `claimed_scope` -- as:
   the member whose `name` equals `stem` exactly, if one does; otherwise
   the member with the strictly SHORTER `name` (ties broken
   alphabetically, though no case in this corpus ties). The LOSER's probe
   `claimed_scope` becomes `f"{stem}/{suffix}"`, where `suffix` is the
   loser's own `name` with the leading `stem` prefix -- and one following
   `-`, if present -- stripped.

This is a pure function of `name`/`claimed_scope` strings alone: it never
reads `outcome_class`, `type`, `detector:`, or any other field a verdict
could leak through, so (per
`scripts/annotate_shadow_parity_subject_scope.py`'s own definition of
"label-blind", which this rule inherits) it cannot be tuned toward or away
from any particular verdict.

**Methodological note.** This rule is transcribed near-verbatim from the
example the issue itself proposes ("the member whose name equals the shared
stem -- or, failing that, the member with the shortest name -- keeps the
bare stem"). `gate1_separator_relations` was run against its output only
AFTER this docstring and `probe_claimed_scope` below were written -- see
`tests/test_comparator_merge_class_scope.py` and
`measurements/comparator-merge-class-scope-2026-09-15.md` for what that run
found. No case's suffix-vs-stem assignment was adjusted after seeing a
relation or a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.comparator import DEFAULT_REGISTRY, gate1_separator_relations
from athenaeum.shadow_parity import ParityCase, load_parity_cases

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CORPORA = (
    REPO_ROOT / "tests/evals/data/detector/cases.subject-scope.yaml",
    REPO_ROOT / "tests/evals/data/resolver/cases.subject-scope.yaml",
)


def probe_claimed_scope(name_a: str, name_b: str, stem: str) -> tuple[str, str]:
    """Return `(probe_claimed_scope_a, probe_claimed_scope_b)` per the rule
    documented in this module's docstring. Callers pass the two members'
    EXISTING `claimed_scope` value as `stem` only when both already agree
    (a real shared stem exists); when they differ, do not call this --
    keep the existing values (case 1 above)."""
    if name_a == stem:
        winner_is_a = True
    elif name_b == stem:
        winner_is_a = False
    elif len(name_a) != len(name_b):
        winner_is_a = len(name_a) < len(name_b)
    else:
        winner_is_a = name_a < name_b

    winner_name, loser_name = (name_a, name_b) if winner_is_a else (name_b, name_a)
    suffix = loser_name[len(stem) :]
    if suffix.startswith("-"):
        suffix = suffix[1:]
    loser_scope = f"{stem}/{suffix}"
    return (stem, loser_scope) if winner_is_a else (loser_scope, stem)


@dataclass(frozen=True)
class ProbeRow:
    """One row of the Gate-1 relation table (athenaeum#1664 Plan step 2)."""

    source: str
    case_id: str
    outcome_class: str
    name_a: str
    name_b: str
    probe_scope_a: str
    probe_scope_b: str
    valid_time: str
    scope: str
    subject: str


def _probe_meta(case: ParityCase) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    assert len(case.members) == 2, f"{case.case_id}: probe rule assumes exactly 2 members"
    member_a, member_b = case.members
    name_a = str(member_a.frontmatter["name"])
    name_b = str(member_b.frontmatter["name"])
    cs_a = str(member_a.frontmatter.get("claimed_scope"))
    cs_b = str(member_b.frontmatter.get("claimed_scope"))
    meta_a = dict(member_a.frontmatter)
    meta_b = dict(member_b.frontmatter)
    if cs_a == cs_b:
        probe_a, probe_b = probe_claimed_scope(name_a, name_b, cs_a)
    else:
        probe_a, probe_b = cs_a, cs_b
    meta_a["claimed_scope"] = probe_a
    meta_b["claimed_scope"] = probe_b
    return meta_a, meta_b, probe_a, probe_b


def compute_probe_table(corpora: tuple[Path, ...] = CORPORA) -> list[ProbeRow]:
    """Load the 18 committed cases, apply the probe rule to a throwaway copy
    of each pair's frontmatter, and tabulate `gate1_separator_relations`
    (`src/athenaeum/comparator.py`) -- zero LLM calls, zero writes back to
    the committed fixtures."""
    rows: list[ProbeRow] = []
    for path in corpora:
        source = path.parent.name
        for case in load_parity_cases(path, source=source):
            meta_a, meta_b, probe_a, probe_b = _probe_meta(case)
            rels = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b)
            rows.append(
                ProbeRow(
                    source=source,
                    case_id=case.case_id,
                    outcome_class=case.outcome_class,
                    name_a=str(case.members[0].frontmatter["name"]),
                    name_b=str(case.members[1].frontmatter["name"]),
                    probe_scope_a=probe_a,
                    probe_scope_b=probe_b,
                    valid_time=rels.get("valid-time", "n/a"),
                    scope=rels.get("scope", "n/a"),
                    subject=rels.get("subject", "n/a"),
                )
            )
    return rows
