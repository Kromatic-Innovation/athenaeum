# SPDX-License-Identifier: Apache-2.0
"""Zero-model-call discriminator for athenaeum#1664: is the 2026-09-15 shadow-parity
run's 0/2 on `merge`-class clusters (`measurements/shadow-parity-2026-09-15.md`)
a fixture artifact or a comparator defect?

New file (not `tests/test_comparator.py`) per athenaeum#1664's implementation
guidance, to avoid colliding with other lanes editing that file. Every
``ComparatorPage``/fake-client helper below is a local copy of
``tests/test_comparator.py``'s own convention (``_page`` /
:func:`_fake_client` / :func:`_content_payload`) -- duplicated, not
imported, matching how every other ``test_*.py`` module in this repo
already keeps its own copy (no shared ``conftest.py`` helper exists for
these). No live network, no ``ANTHROPIC_API_KEY`` read, no model call: the
"client" is a ``unittest.mock.MagicMock`` mirroring
``anthropic.Anthropic().messages.create``'s response shape, exactly as
``tests/test_comparator.py`` and ``tests/test_contradictions.py`` already
do it.

**What this file proves (issue athenaeum#1664 ACs 1-2):**

- :class:`TestEqualClaimedScopeFallsThroughToContradiction` -- the two real
  `merge`-class cases (`refinement_editor_general_and_csv`,
  `propose_merge_ticketing_general_and_exception`), reconstructed with their
  COMMITTED (equal) `claimed_scope`, reach `compare_pages`'s fall-through
  `contradiction` return (`comparator.py:925`) on a stubbed CONFLICTING
  content relation -- never the OVERLAPS branch (`route` stays ``None``,
  not ``"queue"``).
- :class:`TestContainingClaimedScopeReachesSpecialization` -- the SAME two
  pairs, with `claimed_scope` changed to a containing shape
  (`docs-tool` / `docs-tool/csv-exception`, `tickets` /
  `tickets/ticketwell-exception`), reach `specialization`
  (`comparator.py:904-916`) instead, with `separator == ["scope"]` and the
  correct `specific_side`. Asserting the specific verdict + separator +
  side (not merely "not contradiction") is deliberate: a broken
  `_strict_containment` (e.g. always `False`) or a broken
  `compare_hierarchy` (e.g. CONTAINS misread as EQUAL, or the direction of
  `specific_side` flipped) fails this test, not just a looser one.
- :class:`TestProbeGate1RelationTable` -- runs the label-blind probe rule
  from `tests/evals/data/probe_subject_scope_containment.py` over all 18
  committed cases and pins the actual Gate-1 relations found (see that
  module's docstring for the rule, and
  `measurements/comparator-merge-class-scope-2026-09-15.md` for the finding
  this table feeds).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_SPECIALIZATION,
    ComparatorPage,
    ContentRelation,
    compare_pages,
    page_from_text,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_MODULE_PATH = _REPO_ROOT / "tests" / "evals" / "data" / "probe_subject_scope_containment.py"

_spec = importlib.util.spec_from_file_location(
    "probe_subject_scope_containment", _PROBE_MODULE_PATH
)
assert _spec and _spec.loader
probe_subject_scope_containment = importlib.util.module_from_spec(_spec)
# Registered in sys.modules BEFORE exec: the module defines a frozen
# dataclass, and `dataclasses._process_class` looks up
# `sys.modules[cls.__module__]` while building it -- without this line the
# import raises `AttributeError: 'NoneType' object has no attribute
# '__dict__'` (confirmed while writing this test).
sys.modules[_spec.name] = probe_subject_scope_containment
_spec.loader.exec_module(probe_subject_scope_containment)

compute_probe_table = probe_subject_scope_containment.compute_probe_table
probe_claimed_scope = probe_subject_scope_containment.probe_claimed_scope


# ---------------------------------------------------------------------------
# Helpers (local copies of tests/test_comparator.py's conventions)
# ---------------------------------------------------------------------------


def _page(
    page_id: str,
    *,
    subject: str | None = None,
    claimed_scope: str | None = None,
    body: str = "some claim text",
) -> ComparatorPage:
    lines = ["---", "name: probe", "type: feedback"]
    if subject is not None:
        lines.append(f"subject: {subject}")
    if claimed_scope is not None:
        lines.append(f"claimed_scope: {claimed_scope}")
    lines.append('recorded_at: "2026-01-01T00:00:00+00:00"')
    lines.append("---")
    text = "\n".join(lines) + "\n" + body + "\n"
    return page_from_text(page_id, text)


def _fake_client(payload_json: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_json)]
    client.messages.create.return_value = response
    return client


def _content_payload(relation: str, *, passages: list[str] | None = None) -> str:
    return json.dumps(
        {
            "content_relation": relation,
            "conflicting_passages": passages or [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": "test rationale",
        }
    )


# The two real `merge`-class members, per
# tests/evals/data/resolver/cases.subject-scope.yaml:53-90 and :231-269 --
# body text and committed `subject`/`claimed_scope` transcribed verbatim.
_DOCS_TOOL_GENERAL_BODY = "The consulting team uses Pagemoor for all client-facing project docs."
_DOCS_TOOL_EXCEPTION_BODY = "Client-facing project CSVs live in Tallyfold, not Pagemoor."
_TICKETS_GENERAL_BODY = "The team tracks all engineering work in Linear."
_TICKETS_EXCEPTION_BODY = "Support-desk bug reports are logged in Ticketwell, not Linear."


class TestEqualClaimedScopeFallsThroughToContradiction:
    """AC1: `compare_pages` on a CONFLICTING stubbed pair returns
    `contradiction` when `claimed_scope` is equal on both members -- the
    shape every real cluster in `cases.subject-scope.yaml` has today."""

    def test_docs_tool_pair_equal_scope_is_contradiction(self) -> None:
        page_a = _page(
            "docs-tool-pagemoor", subject="docs-tool", claimed_scope="docs-tool",
            body=_DOCS_TOOL_GENERAL_BODY,
        )
        page_b = _page(
            "docs-tool-csv-exception", subject="docs-tool", claimed_scope="docs-tool",
            body=_DOCS_TOOL_EXCEPTION_BODY,
        )
        client = _fake_client(
            _content_payload(ContentRelation.CONFLICTING, passages=["a", "b"])
        )
        outcome = compare_pages(page_a, page_b, client=client)
        assert outcome.verdict == VERDICT_CONTRADICTION
        # Fall-through (:925), never the OVERLAPS branch (:896) -- that
        # branch always sets separator/route, this one sets neither.
        assert outcome.separator == []
        assert outcome.route is None
        client.messages.create.assert_called_once()

    def test_tickets_pair_equal_scope_is_contradiction(self) -> None:
        page_a = _page(
            "tickets-linear", subject="tickets", claimed_scope="tickets",
            body=_TICKETS_GENERAL_BODY,
        )
        page_b = _page(
            "tickets-ticketwell-exception", subject="tickets", claimed_scope="tickets",
            body=_TICKETS_EXCEPTION_BODY,
        )
        client = _fake_client(
            _content_payload(ContentRelation.CONFLICTING, passages=["a", "b"])
        )
        outcome = compare_pages(page_a, page_b, client=client)
        assert outcome.verdict == VERDICT_CONTRADICTION
        assert outcome.separator == []
        assert outcome.route is None


class TestContainingClaimedScopeReachesSpecialization:
    """AC2 counter-example: the SAME two pairs, `claimed_scope` changed to a
    containing shape, return `specialization` -- and fail this test if
    `_strict_containment` or `compare_hierarchy` regresses (wrong verdict,
    wrong separator, or wrong `specific_side` each independently fail an
    assertion here)."""

    def test_docs_tool_pair_containing_scope_is_specialization(self) -> None:
        page_a = _page(
            "docs-tool-pagemoor", subject="docs-tool", claimed_scope="docs-tool",
            body=_DOCS_TOOL_GENERAL_BODY,
        )
        page_b = _page(
            "docs-tool-csv-exception",
            subject="docs-tool",
            claimed_scope="docs-tool/csv-exception",
            body=_DOCS_TOOL_EXCEPTION_BODY,
        )
        client = _fake_client(
            _content_payload(ContentRelation.CONFLICTING, passages=["a", "b"])
        )
        outcome = compare_pages(page_a, page_b, client=client)
        assert outcome.verdict == VERDICT_SPECIALIZATION
        assert outcome.separator == ["scope"]
        assert outcome.specific_side == "b"  # docs-tool/csv-exception is the specific side

    def test_tickets_pair_containing_scope_is_specialization(self) -> None:
        page_a = _page(
            "tickets-linear", subject="tickets", claimed_scope="tickets",
            body=_TICKETS_GENERAL_BODY,
        )
        page_b = _page(
            "tickets-ticketwell-exception",
            subject="tickets",
            claimed_scope="tickets/ticketwell-exception",
            body=_TICKETS_EXCEPTION_BODY,
        )
        client = _fake_client(
            _content_payload(ContentRelation.CONFLICTING, passages=["a", "b"])
        )
        outcome = compare_pages(page_a, page_b, client=client)
        assert outcome.verdict == VERDICT_SPECIALIZATION
        assert outcome.separator == ["scope"]
        assert outcome.specific_side == "b"

    def test_reversed_member_order_flips_specific_side(self) -> None:
        """Direction is read from the raw coordinates, not argument order
        (module docstring of `_specific_side`) -- swap `page_a`/`page_b`
        and the specific side swaps too."""
        page_general = _page(
            "docs-tool-pagemoor", subject="docs-tool", claimed_scope="docs-tool",
            body=_DOCS_TOOL_GENERAL_BODY,
        )
        page_specific = _page(
            "docs-tool-csv-exception",
            subject="docs-tool",
            claimed_scope="docs-tool/csv-exception",
            body=_DOCS_TOOL_EXCEPTION_BODY,
        )
        client = _fake_client(
            _content_payload(ContentRelation.CONFLICTING, passages=["a", "b"])
        )
        outcome = compare_pages(page_specific, page_general, client=client)
        assert outcome.verdict == VERDICT_SPECIALIZATION
        assert outcome.specific_side == "a"


class TestProbeRuleIsLabelBlind:
    """The probe rule (`tests/evals/data/probe_subject_scope_containment.py`)
    is a pure function of `name`/`claimed_scope` strings -- never
    `outcome_class`."""

    def test_winner_is_the_name_matching_the_stem(self) -> None:
        assert probe_claimed_scope("standup-time", "standup-time-updated", "standup-time") == (
            "standup-time",
            "standup-time/updated",
        )

    def test_winner_is_the_shorter_name_when_neither_matches_the_stem(self) -> None:
        assert probe_claimed_scope("docs-tool-pagemoor", "docs-tool-tallyfold", "docs-tool") == (
            "docs-tool",
            "docs-tool/tallyfold",
        )

    def test_no_shared_stem_makes_no_change(self) -> None:
        # meeting_cadence_different_scenarios: cs_a != cs_b, so callers never
        # invoke probe_claimed_scope for this case at all -- see
        # `_probe_meta` in the probe module. This test documents that
        # invariant at the call-site level, not this function's own
        # behaviour on a same-stem input.
        rows = compute_probe_table()
        row = next(r for r in rows if r.case_id == "meeting_cadence_different_scenarios")
        assert row.probe_scope_a == "client-weekly-sync"
        assert row.probe_scope_b == "internal-monthly"


class TestProbeGate1RelationTable:
    """athenaeum#1664 Plan step 2: tabulate Gate-1 relations for all 18 cases
    under the probe rule, zero LLM calls, and check whether any `contradict`
    case is falsely CONTAINS (the comparator-defect signal named by the
    issue). See `measurements/comparator-merge-class-scope-2026-09-15.md`
    for the finding this table feeds and the recorded verdict."""

    def test_covers_all_eighteen_committed_cases(self) -> None:
        rows = compute_probe_table()
        assert len(rows) == 18
        assert {r.outcome_class for r in rows} == {"pass", "contradict", "escalate", "merge"}

    def test_both_merge_cases_reach_contains_under_the_probe_rule(self) -> None:
        rows = compute_probe_table()
        merge_rows = {r.case_id: r for r in rows if r.outcome_class == "merge"}
        assert set(merge_rows) == {
            "refinement_editor_general_and_csv",
            "propose_merge_ticketing_general_and_exception",
        }
        for row in merge_rows.values():
            assert row.scope == "contains"

    def test_all_five_contradict_cases_are_also_falsely_contains(self) -> None:
        """The probe-rule finding this issue turns on: a purely name-derived,
        label-blind containment rule cannot separate `merge` from
        `contradict` -- EVERY `contradict` case with a real shared name stem
        also reads `scope: contains`, exactly like the two `merge` cases.
        This is pinned as a regression check: if a future change to
        `compare_hierarchy` or the probe rule changes this set, the
        athenaeum#1664 finding needs re-evaluating, not silent staleness."""
        rows = compute_probe_table()
        contradict_rows = {r.case_id: r for r in rows if r.outcome_class == "contradict"}
        assert len(contradict_rows) == 5
        falsely_contains = {cid for cid, r in contradict_rows.items() if r.scope == "contains"}
        assert falsely_contains == set(contradict_rows)
