# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.shadow_parity` (issue athenaeum#1333).

Zero network, zero cost — every LLM "client" here is a
``unittest.mock.MagicMock`` mirroring the Anthropic SDK's
``messages.create`` response shape, the same posture
``tests/test_contradictions.py`` / ``tests/test_cluster_comparator.py`` /
``tests/test_comparator.py`` already establish for this codebase.

Acceptance-criteria map (see the athenaeum#1333 implementation brief):

- AC1 ``--help`` documents ``--dry-run``/``--max-usd``/output path:
  :class:`TestCliHelp`.
- AC2 agreement matrix over a fixture cluster set:
  :class:`TestAgreementMatrixOverFixtures`.
- AC3 multiplier over fixtures with a known call count:
  :class:`TestMultiplierKnownCallCount`.
- AC4 lives in ``tests/evals/test_shadow_parity_recast.py``.
- AC5 ``--dry-run`` makes zero paid calls: :class:`TestDryRunZeroCalls`.
- AC6 ``--max-usd`` abort, both branches: :class:`TestMaxUsdAbort`.
- AC7 report shape: :class:`TestReportShape`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import athenaeum.cli as athenaeum_cli
from athenaeum.comparator import CompareOutcome
from athenaeum.models import ContradictionResult
from athenaeum.shadow_parity import (
    COMPARATOR_VERDICTS,
    DETECTOR_VERDICTS,
    AgreementMatrix,
    ParityCase,
    ParityItem,
    ParityMember,
    ParityProjection,
    ParityReport,
    _corpus_digest_for_cases,
    classify_agreement,
    comparator_decided_correctly,
    detector_verdict_from_result,
    load_parity_cases,
    materialise_members,
    project_shadow_parity,
    render_report,
    roll_up_comparator_verdict,
    run_shadow_parity,
    write_report,
)
from tests.evals.harness import EVAL_DATA_ROOT

# ---------------------------------------------------------------------------
# Shared stub-client helpers
# ---------------------------------------------------------------------------


def _canned_response(
    payload: dict[str, Any], *, input_tokens: int = 10, output_tokens: int = 5
) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps(payload))]
    response.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _uniform_client(payload: dict[str, Any], **token_kwargs: int) -> MagicMock:
    """A stub client that returns the SAME canned response for every call."""
    client = MagicMock()
    client.messages.create.return_value = _canned_response(payload, **token_kwargs)
    return client


def _keyed_client(responses_by_marker: dict[str, dict[str, Any]]) -> MagicMock:
    """A stub client whose response depends on which case's MARKER string
    appears in the outgoing prompt — robust to call order, unlike a
    positional ``side_effect`` list."""
    client = MagicMock()

    def _create(**params: Any) -> MagicMock:
        text = str(params.get("messages"))
        for marker, payload in responses_by_marker.items():
            if marker in text:
                return _canned_response(payload)
        raise AssertionError(f"no canned response configured for prompt: {text[:300]!r}")

    client.messages.create.side_effect = _create
    return client


def _detector_payload(*, detected: bool, conflict_type: str | None = None) -> dict[str, Any]:
    return {
        "detected": detected,
        "conflict_type": conflict_type,
        "members_involved": [],
        "conflicting_passages": [],
        "rationale": "test rationale",
    }


def _content_relation_payload(relation: str) -> dict[str, Any]:
    return {
        "content_relation": relation,
        "conflicting_passages": [] if relation != "conflicting" else ["a", "b"],
        "predicate_a": "predicate-a",
        "predicate_b": "predicate-b",
        "rationale": "test rationale",
    }


def _member(filename: str, body: str, frontmatter: dict[str, Any]) -> ParityMember:
    return ParityMember(filename=filename, body=body, frontmatter=frontmatter)


def _case(
    case_id: str,
    members: tuple[ParityMember, ...],
    *,
    outcome_class: str = "pass",
    declared_detector: Any = None,
    source: str = "fixture",
) -> ParityCase:
    return ParityCase(
        case_id=case_id,
        outcome_class=outcome_class,
        members=members,
        declared_detector=declared_detector,
        source=source,
    )


def _sized_case(case_id: str, n_members: int) -> ParityCase:
    members = tuple(
        _member(
            f"m{i}.md", f"body {i} of {case_id}", {"type": "feedback", "name": f"{case_id}-{i}"}
        )
        for i in range(n_members)
    )
    return _case(case_id, members)


# ---------------------------------------------------------------------------
# load_parity_cases
# ---------------------------------------------------------------------------


class TestLoadParityCases:
    def test_detector_corpus_has_ten_cases_no_declared_detector(self) -> None:
        cases = load_parity_cases(EVAL_DATA_ROOT / "detector" / "cases.yaml")
        assert len(cases) == 10
        expected_ids = [
            "standup_time",
            "invoice_cadence_refinement",
            "deploy_target_sequential_snapshot",
            "office_address_undated",
            "expense_reimbursement_receipts",
            "client_owner_meridian_pass_1",
            "tool_choice_editor",
            "meeting_cadence_different_scenarios",
            "budget_approver_undated",
            "pto_policy_restatement",
        ]
        assert [c.case_id for c in cases] == expected_ids
        for c in cases:
            assert c.source == "detector"
            assert c.declared_detector is None
            assert c.outcome_class in ("pass", "contradict", "escalate")
            assert len(c.members) >= 1

    def test_resolver_corpus_has_eight_cases_all_declared_detector(self) -> None:
        cases = load_parity_cases(EVAL_DATA_ROOT / "resolver" / "cases.yaml")
        assert len(cases) == 8
        expected_ids = [
            "refinement_editor_general_and_csv",
            "restatement_pto_days",
            "decision_conflict_hosting_migration",
            "undated_office_address",
            "undated_budget_approver",
            "sequential_snapshot_headcount",
            "precedence_user_over_unsourced_contact",
            "propose_merge_ticketing_general_and_exception",
        ]
        assert [c.case_id for c in cases] == expected_ids
        for c in cases:
            assert c.source == "resolver"
            assert c.declared_detector is not None
            assert c.declared_detector.conflict_type in ("factual", "prescriptive", "stance")
            assert c.outcome_class in ("pass", "contradict", "escalate", "merge")

    def test_source_override(self) -> None:
        cases = load_parity_cases(EVAL_DATA_ROOT / "detector" / "cases.yaml", source="custom")
        assert all(c.source == "custom" for c in cases)


# ---------------------------------------------------------------------------
# materialise_members
# ---------------------------------------------------------------------------


class TestMaterialiseMembers:
    def test_writes_frontmatter_and_body_round_trip(self, tmp_path: Path) -> None:
        case = _case(
            "c1",
            (_member("a.md", "the body text", {"type": "project", "name": "a-name"}),),
        )
        members = materialise_members(case, tmp_path / "scope")
        assert len(members) == 1
        am = members[0]
        assert am.path.is_file()
        assert am.memory_type == "project"
        assert am.name == "a-name"
        text = am.path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "the body text" in text


# ---------------------------------------------------------------------------
# _corpus_digest_for_cases (QA finding 3 -- was 100% line-covered, zero
# behavioural test; its value is the provenance stamp on every report)
# ---------------------------------------------------------------------------


class TestCorpusDigestForCases:
    def test_deterministic_for_the_same_cases(self) -> None:
        cases = [_sized_case("a", 2), _sized_case("b", 3)]
        assert _corpus_digest_for_cases(cases) == _corpus_digest_for_cases(cases)
        # A freshly-built, content-identical case list (not the same objects)
        # must also match -- determinism is about CONTENT, not identity.
        cases_again = [_sized_case("a", 2), _sized_case("b", 3)]
        assert _corpus_digest_for_cases(cases) == _corpus_digest_for_cases(cases_again)

    def test_sensitive_to_a_changed_member_body(self) -> None:
        base = [_case("c1", (_member("a.md", "original body", {"type": "feedback"}),))]
        changed = [_case("c1", (_member("a.md", "DIFFERENT body", {"type": "feedback"}),))]
        assert _corpus_digest_for_cases(base) != _corpus_digest_for_cases(changed)

    def test_sensitive_to_changed_frontmatter_with_body_held_constant(self) -> None:
        base = [_case("c1", (_member("a.md", "same body", {"type": "feedback"}),))]
        changed = [_case("c1", (_member("a.md", "same body", {"type": "project"}),))]
        assert _corpus_digest_for_cases(base) != _corpus_digest_for_cases(changed)

    def test_sensitive_to_the_case_set_changing(self) -> None:
        one_case = [_sized_case("a", 2)]
        two_cases = [_sized_case("a", 2), _sized_case("b", 2)]
        assert _corpus_digest_for_cases(one_case) != _corpus_digest_for_cases(two_cases)

    def test_insensitive_to_frontmatter_key_insertion_order(self) -> None:
        """Two semantically-identical frontmatter dicts that merely differ
        in key order must hash equal -- the digest is a content address,
        not an incidental artifact of dict construction order."""
        fm_a = {"type": "feedback", "source_type": "user-stated"}
        fm_b = {"source_type": "user-stated", "type": "feedback"}
        assert fm_a == fm_b  # same content, order asserted different below
        first = [_case("c1", (_member("a.md", "body", fm_a),))]
        second = [_case("c1", (_member("a.md", "body", fm_b),))]
        assert _corpus_digest_for_cases(first) == _corpus_digest_for_cases(second)


# ---------------------------------------------------------------------------
# detector_verdict_from_result
# ---------------------------------------------------------------------------


class TestDetectorVerdictFromResult:
    def test_incomplete_is_unavailable_even_when_detected(self) -> None:
        result = ContradictionResult(detected=True, conflict_type="factual", incomplete=True)
        assert detector_verdict_from_result(result) == "unavailable"

    def test_llm_unavailable_rationale_is_unavailable_not_not_detected(self) -> None:
        """QA follow-up: a live run with no ANTHROPIC_API_KEY set (client=None)
        produces exactly this result shape -- detected=False,
        rationale="llm-unavailable", incomplete=False (client=None never
        sets incomplete). This must NOT read as "not-detected": a lane that
        never ran is an absent answer, not a genuine finding of no
        contradiction."""
        result = ContradictionResult(detected=False, rationale="llm-unavailable")
        assert detector_verdict_from_result(result) == "unavailable"

    def test_non_transient_call_failure_is_also_unavailable(self) -> None:
        """detect_contradictions' non-transient-failure fallback ALSO
        returns rationale="llm-unavailable" with incomplete=False (only the
        exhausted-retries path sets incomplete=True) -- both must map the
        same way."""
        result = ContradictionResult(
            detected=False, rationale="llm-unavailable", incomplete=False
        )
        assert detector_verdict_from_result(result) == "unavailable"

    def test_singleton_is_not_detected_not_unavailable(self) -> None:
        """A one-member cluster genuinely cannot contradict itself -- a
        structural fact, not a degradation, so rationale="singleton" must
        stay "not-detected" even though it shares detected=False with the
        llm-unavailable case."""
        result = ContradictionResult(detected=False, rationale="singleton")
        assert detector_verdict_from_result(result) == "not-detected"

    def test_genuine_not_detected(self) -> None:
        result = ContradictionResult(detected=False, rationale="no conflict found")
        assert detector_verdict_from_result(result) == "not-detected"

    @pytest.mark.parametrize("conflict_type", ["factual", "prescriptive", "stance"])
    def test_known_conflict_type(self, conflict_type: str) -> None:
        result = ContradictionResult(detected=True, conflict_type=conflict_type)  # type: ignore[arg-type]
        assert detector_verdict_from_result(result) == conflict_type

    def test_detected_untyped(self) -> None:
        result = ContradictionResult(detected=True, conflict_type=None)
        assert detector_verdict_from_result(result) == "detected-untyped"


# ---------------------------------------------------------------------------
# roll_up_comparator_verdict
# ---------------------------------------------------------------------------


def _outcome(verdict: str | None) -> CompareOutcome:
    return CompareOutcome(verdict=verdict)


class TestRollUpComparatorVerdict:
    def test_empty_is_no_decision(self) -> None:
        assert roll_up_comparator_verdict([]) == "no-decision"

    def test_all_none_is_no_decision(self) -> None:
        assert roll_up_comparator_verdict([_outcome(None), _outcome(None)]) == "no-decision"

    def test_single_pair_passes_through(self) -> None:
        assert roll_up_comparator_verdict([_outcome("distinct")]) == "distinct"

    def test_precedence_contradiction_beats_everything(self) -> None:
        outcomes = [
            _outcome("distinct"),
            _outcome("duplicate"),
            _outcome("specialization"),
            _outcome("underdetermined"),
            _outcome("contradiction"),
        ]
        assert roll_up_comparator_verdict(outcomes) == "contradiction"

    def test_precedence_underdetermined_beats_specialization_duplicate_distinct(self) -> None:
        outcomes = [
            _outcome("distinct"),
            _outcome("duplicate"),
            _outcome("specialization"),
            _outcome("underdetermined"),
        ]
        assert roll_up_comparator_verdict(outcomes) == "underdetermined"

    def test_precedence_specialization_beats_duplicate_distinct(self) -> None:
        outcomes = [_outcome("distinct"), _outcome("duplicate"), _outcome("specialization")]
        assert roll_up_comparator_verdict(outcomes) == "specialization"

    def test_precedence_duplicate_beats_distinct(self) -> None:
        outcomes = [_outcome("distinct"), _outcome("duplicate")]
        assert roll_up_comparator_verdict(outcomes) == "duplicate"

    def test_none_entries_excluded_not_outranking(self) -> None:
        assert roll_up_comparator_verdict([_outcome(None), _outcome("distinct")]) == "distinct"


# ---------------------------------------------------------------------------
# classify_agreement
# ---------------------------------------------------------------------------


class TestClassifyAgreement:
    def test_total_over_full_cross_product(self) -> None:
        for d in DETECTOR_VERDICTS:
            for c in COMPARATOR_VERDICTS:
                result = classify_agreement(d, c)
                assert result in ("agree", "disagree", "inconclusive"), (d, c, result)

    def test_unavailable_detector_is_always_inconclusive(self) -> None:
        for c in COMPARATOR_VERDICTS:
            assert classify_agreement("unavailable", c) == "inconclusive"

    def test_no_decision_comparator_is_always_inconclusive(self) -> None:
        for d in DETECTOR_VERDICTS:
            assert classify_agreement(d, "no-decision") == "inconclusive"

    def test_underdetermined_comparator_is_always_inconclusive(self) -> None:
        for d in DETECTOR_VERDICTS:
            assert classify_agreement(d, "underdetermined") == "inconclusive"

    @pytest.mark.parametrize("comparator_verdict", ["distinct", "duplicate", "specialization"])
    def test_not_detected_agrees_with_non_conflict_verdicts(self, comparator_verdict: str) -> None:
        assert classify_agreement("not-detected", comparator_verdict) == "agree"

    def test_not_detected_disagrees_with_contradiction(self) -> None:
        assert classify_agreement("not-detected", "contradiction") == "disagree"

    @pytest.mark.parametrize(
        "detector_verdict", ["factual", "prescriptive", "stance", "detected-untyped"]
    )
    def test_detected_agrees_only_with_contradiction(self, detector_verdict: str) -> None:
        assert classify_agreement(detector_verdict, "contradiction") == "agree"
        assert classify_agreement(detector_verdict, "distinct") == "disagree"


# ---------------------------------------------------------------------------
# comparator_decided_correctly
# ---------------------------------------------------------------------------


class TestComparatorDecidedCorrectly:
    def test_unknown_outcome_class_is_not_scored(self) -> None:
        assert comparator_decided_correctly("bogus", "distinct") is None

    def test_no_decision_is_not_scored(self) -> None:
        assert comparator_decided_correctly("pass", "no-decision") is None

    def test_pass_scored_correctly(self) -> None:
        assert comparator_decided_correctly("pass", "distinct") is True
        assert comparator_decided_correctly("pass", "contradiction") is False

    def test_merge_scored_correctly(self) -> None:
        assert comparator_decided_correctly("merge", "specialization") is True
        assert comparator_decided_correctly("merge", "distinct") is False


# ---------------------------------------------------------------------------
# AC2 — agreement matrix over a fixture cluster set
# ---------------------------------------------------------------------------


class TestAgreementMatrixOverFixtures:
    """A small hand-built cluster set exercising all three
    :func:`classify_agreement` buckets and multiple values in both verdict
    spaces, run through the REAL :func:`run_shadow_parity` pipeline (stub
    LLM clients only — the detector/comparator/Gate-1 logic itself is
    exercised for real, matching ``tests/test_comparator.py``'s own
    frontmatter recipes for forcing a specific Gate-1 relation).
    """

    def test_matrix_counts_rate_and_both_spaces_represented(self, tmp_path: Path) -> None:
        # Case A: Gate-1 DISJOINT valid-time windows -> DISTINCT, ZERO
        # comparator LLM call. Detector: not-detected. -> agree.
        case_a = _case(
            "case_a",
            (
                _member(
                    "a1.md",
                    "CASE_A body one",
                    {"type": "feedback", "valid_from": "2020-01-01", "valid_until": "2020-06-30"},
                ),
                _member(
                    "a2.md",
                    "CASE_A body two",
                    {"type": "feedback", "valid_from": "2021-01-01", "valid_until": "2021-06-30"},
                ),
            ),
            outcome_class="pass",
        )
        # Case B: subject/scope/valid-time held IDENTICAL (-> EQUAL, not
        # UNKNOWN) so a "conflicting" Gate-2 verdict exits plain
        # CONTRADICTION (mirrors tests/test_comparator.py's own recipe for
        # this exit). Detector: factual (a DETECTED verdict). -> agree.
        _shared_fm = {
            "type": "feedback",
            "subject": "acme-corp",
            "claimed_scope": "engineering",
            "valid_from": "2026-01-01",
            "valid_until": "2026-12-31",
        }
        case_b = _case(
            "case_b",
            (
                _member("b1.md", "CASE_B body one", dict(_shared_fm)),
                _member("b2.md", "CASE_B body two", dict(_shared_fm)),
            ),
            outcome_class="contradict",
        )
        # Case C: same Gate-1 shape as B (-> CONTRADICTION), but detector
        # says not-detected -> disagree.
        case_c = _case(
            "case_c",
            (
                _member("c1.md", "CASE_C body one", dict(_shared_fm)),
                _member("c2.md", "CASE_C body two", dict(_shared_fm)),
            ),
            outcome_class="pass",
        )
        # Case D: minimal frontmatter (subject/scope/valid-time all absent
        # -> UNKNOWN on both sides) so "conflicting" exits UNDERDETERMINED.
        # Detector: prescriptive (DETECTED). comparator "underdetermined"
        # is always inconclusive regardless of the detector side.
        case_d = _case(
            "case_d",
            (
                _member("d1.md", "CASE_D body one", {"type": "feedback"}),
                _member("d2.md", "CASE_D body two", {"type": "feedback"}),
            ),
            outcome_class="escalate",
        )

        detector_client = _keyed_client(
            {
                "CASE_A": _detector_payload(detected=False),
                "CASE_B": _detector_payload(detected=True, conflict_type="factual"),
                "CASE_C": _detector_payload(detected=False),
                "CASE_D": _detector_payload(detected=True, conflict_type="prescriptive"),
            }
        )
        comparator_client = _keyed_client(
            {
                "CASE_B": _content_relation_payload("conflicting"),
                "CASE_C": _content_relation_payload("conflicting"),
                "CASE_D": _content_relation_payload("conflicting"),
            }
        )

        report = run_shadow_parity(
            [case_a, case_b, case_c, case_d],
            detector_client=detector_client,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        by_id = {item.case_id: item for item in report.items}
        assert by_id["case_a"].detector_verdict == "not-detected"
        assert by_id["case_a"].comparator_verdict == "distinct"
        assert by_id["case_a"].agreement == "agree"
        # comparator_calls counts calls actually DISPATCHED (QA finding B),
        # NOT pairs merely processed -- case_a's one pair is Gate-1-resolved
        # (disjoint valid-time windows), so it costs zero dispatches even
        # though it has a real verdict in pair_verdicts.
        assert by_id["case_a"].comparator_calls == 0
        assert comparator_client.messages.create.call_count == 3  # cases B, C, D only

        assert by_id["case_b"].detector_verdict == "factual"
        assert by_id["case_b"].comparator_verdict == "contradiction"
        assert by_id["case_b"].agreement == "agree"

        assert by_id["case_c"].detector_verdict == "not-detected"
        assert by_id["case_c"].comparator_verdict == "contradiction"
        assert by_id["case_c"].agreement == "disagree"

        assert by_id["case_d"].detector_verdict == "prescriptive"
        assert by_id["case_d"].comparator_verdict == "underdetermined"
        assert by_id["case_d"].agreement == "inconclusive"

        # Report-level comparator_calls totals only the 3 DISPATCHED pairs
        # (B, C, D) -- case_a's Gate-1-resolved pair is excluded.
        assert report.comparator_calls == 3

        matrix = report.matrix
        assert matrix.counts[("not-detected", "distinct")] == 1
        assert matrix.counts[("factual", "contradiction")] == 1
        assert matrix.counts[("not-detected", "contradiction")] == 1
        assert matrix.counts[("prescriptive", "underdetermined")] == 1
        assert matrix.total == 4
        assert matrix.agree_count == 2
        assert matrix.disagree_count == 1
        assert matrix.inconclusive_count == 1
        assert matrix.agreement_rate == pytest.approx(2 / 3)

        # Both verdict spaces are represented by more than one value.
        detector_verdicts_seen = {item.detector_verdict for item in report.items}
        comparator_verdicts_seen = {item.comparator_verdict for item in report.items}
        assert detector_verdicts_seen == {"not-detected", "factual", "prescriptive"}
        assert comparator_verdicts_seen == {"distinct", "contradiction", "underdetermined"}

    def test_declared_detector_case_costs_zero_detector_calls(self, tmp_path: Path) -> None:
        """A resolver-suite-shaped case (a ``declared_detector``) must never
        dispatch a detector call, through :func:`run_shadow_parity` itself
        -- proven with a client that raises if touched."""
        from athenaeum.shadow_parity import DeclaredDetectorVerdict

        case = _case(
            "declared_case",
            (
                _member("x1.md", "declared body one", {"type": "feedback"}),
                _member("x2.md", "declared body two", {"type": "feedback"}),
            ),
            outcome_class="contradict",
            declared_detector=DeclaredDetectorVerdict(
                conflict_type="factual", rationale="pre-declared", passages=["p1", "p2"]
            ),
        )

        def _exploding_detector_client() -> MagicMock:
            client = MagicMock()
            client.messages.create.side_effect = AssertionError(
                "a declared-detector case must never dispatch a detector call"
            )
            return client

        comparator_client = _uniform_client(_content_relation_payload("compatible"))

        report = run_shadow_parity(
            [case],
            detector_client=_exploding_detector_client(),
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        assert len(report.items) == 1
        item = report.items[0]
        assert item.detector_calls == 0
        assert item.detector_verdict == "factual"
        assert report.detector_calls == 0


# ---------------------------------------------------------------------------
# AC3 — multiplier over fixtures with a known call count
# ---------------------------------------------------------------------------


class TestMultiplierKnownCallCount:
    def test_sizes_2_3_4_yield_10_comparator_calls_and_multiplier(self, tmp_path: Path) -> None:
        cases = [_sized_case("size2", 2), _sized_case("size3", 3), _sized_case("size4", 4)]

        detector_client = _uniform_client(_detector_payload(detected=False))
        comparator_client = _uniform_client(_content_relation_payload("compatible"))

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        assert report.detector_calls == 3
        assert report.comparator_calls == 10  # C(2,2)+C(3,2)+C(4,2) = 1+3+6
        assert report.call_multiplier == pytest.approx(10 / 3)
        assert detector_client.messages.create.call_count == 3
        assert comparator_client.messages.create.call_count == 10

    def test_measured_multiplier_matches_projected_when_every_call_is_made(
        self, tmp_path: Path
    ) -> None:
        """QA finding B: measured and projected multipliers must be
        computed the SAME way. This fixture has no dimension coordinates on
        any member (no subject/claimed_scope/valid_from), so Gate 1 never
        short-circuits a pair -- every planned pair actually dispatches,
        which is exactly the condition under which the pre-run WORST-CASE
        projection (raw pair count) and the post-run MEASURED count
        (calls actually issued) must coincide."""
        cases = [_sized_case("size2", 2), _sized_case("size3", 3)]
        detector_client = _uniform_client(_detector_payload(detected=False))
        comparator_client = _uniform_client(_content_relation_payload("compatible"))

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        assert report.detector_calls == report.projection.projected_detector_calls
        assert report.comparator_calls == report.projection.projected_comparator_calls
        assert report.call_multiplier == report.projection.projected_multiplier
        assert report.call_multiplier == pytest.approx(4 / 2)  # (1+3) pairs / 2 clusters


# ---------------------------------------------------------------------------
# AC5 — --dry-run makes zero paid calls
# ---------------------------------------------------------------------------


class TestDryRunZeroCalls:
    def test_project_shadow_parity_takes_no_client_argument(self, tmp_path: Path) -> None:
        cases = [_sized_case("s2", 2), _sized_case("s3", 3)]
        projection = project_shadow_parity(cases, workdir=tmp_path)
        assert isinstance(projection, ParityProjection)
        assert projection.projected_detector_calls == 2
        assert projection.projected_comparator_calls == 1 + 3
        assert projection.projected_cost_usd_lower >= 0.0
        assert projection.projected_cost_usd_upper >= projection.projected_cost_usd_lower

    def test_cli_dry_run_never_constructs_a_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cases_path = EVAL_DATA_ROOT / "detector" / "cases.yaml"

        def _exploding_build_llm_client(*args: Any, **kwargs: Any) -> MagicMock:
            client = MagicMock()
            client.messages.create.side_effect = AssertionError(
                "dry-run must never construct/call a live client"
            )
            return client

        monkeypatch.setattr(
            "athenaeum.provider.build_llm_client", _exploding_build_llm_client
        )

        exit_code = athenaeum_cli.main(
            ["measure", "shadow-parity", "--cases", str(cases_path), "--dry-run", "--json"]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["projected_detector_calls"] == 10
        assert isinstance(payload["projected_cost_usd_lower"], float)
        assert isinstance(payload["projected_cost_usd_upper"], float)


# ---------------------------------------------------------------------------
# QA finding 1 — the comparator-gate belts (env override + gate_enabled)
# ---------------------------------------------------------------------------


class TestComparatorGateEnforced:
    """QA finding 1 on athenaeum#1333: :func:`athenaeum.config.resolve_comparator_enabled`
    reads ``ATHENAEUM_COMPARATOR_ENABLED`` FIRST and unconditionally --
    overriding :func:`athenaeum.shadow_parity._with_comparator_forced_on`'s
    yaml-key override. A run that silently never enables the comparator
    must abort, never report a fabricated "zero calls, zero multiplier"
    success indistinguishable from a genuine finding.
    """

    def test_env_var_override_aborts_preflight_not_silently_measures_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_COMPARATOR_ENABLED", "false")
        cases = [_sized_case("s2", 2)]

        def _exploding_client() -> MagicMock:
            client = MagicMock()
            client.messages.create.side_effect = AssertionError("must not be called")
            return client

        report = run_shadow_parity(
            cases,
            detector_client=_exploding_client(),
            comparator_client=_exploding_client(),
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert report.items == []
        assert report.comparator_calls == 0
        assert report.call_multiplier is None
        assert "ATHENAEUM_COMPARATOR_ENABLED" in report.abort_reason

    def test_gate_enabled_false_mid_run_aborts_instead_of_no_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second belt: even when the preflight check passes, a
        per-case ``gate_enabled=False`` must still abort rather than
        silently fold an empty ``outcomes`` into the matrix as a
        fabricated "no-decision"."""
        from athenaeum.cluster_comparator import ClusterComparatorResult

        def _fake_run_cluster_comparator(*args: Any, **kwargs: Any) -> ClusterComparatorResult:
            return ClusterComparatorResult(
                cluster_id=str(kwargs.get("cluster_id", "")), pair_count=1, gate_enabled=False
            )

        monkeypatch.setattr(
            "athenaeum.shadow_parity.run_cluster_comparator", _fake_run_cluster_comparator
        )

        cases = [_sized_case("s2", 2)]
        detector_client = _uniform_client(_detector_payload(detected=False))
        comparator_client = _uniform_client(_content_relation_payload("compatible"))

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert report.items == []
        assert "gate_enabled=False" in report.abort_reason


# ---------------------------------------------------------------------------
# QA follow-up — missing-client preflight (a live run repro: no
# ANTHROPIC_API_KEY -> both clients None -> a "clean" fabricated report)
# ---------------------------------------------------------------------------


class TestMissingClientPreflight:
    """Live-run repro: with no ``ANTHROPIC_API_KEY`` set,
    ``athenaeum.provider.build_llm_client`` returns ``None`` for both
    lanes, and (pre-fix) ``run_shadow_parity`` still produced a report
    claiming ``agreement_rate: 1.000`` -- a parity harness with no model
    client must abort, not measure "nothing" and call it a finding.
    """

    def test_both_clients_none_aborts_rather_than_produces_a_report(
        self, tmp_path: Path
    ) -> None:
        cases = [_sized_case("s2", 2)]
        report = run_shadow_parity(
            cases, detector_client=None, comparator_client=None, workdir=tmp_path
        )
        assert report.aborted is True
        assert report.items == []
        assert "detector_client" in report.abort_reason
        assert "comparator_client" in report.abort_reason

    def test_detector_client_none_alone_aborts(self, tmp_path: Path) -> None:
        cases = [_sized_case("s2", 2)]
        comparator_client = _uniform_client(_content_relation_payload("compatible"))
        report = run_shadow_parity(
            cases,
            detector_client=None,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )
        assert report.aborted is True
        assert report.items == []
        assert "detector_client" in report.abort_reason
        assert "comparator_client" not in report.abort_reason.split("is None")[0]

    def test_comparator_client_none_alone_aborts(self, tmp_path: Path) -> None:
        cases = [_sized_case("s2", 2)]
        detector_client = _uniform_client(_detector_payload(detected=False))
        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=None,
            workdir=tmp_path,
        )
        assert report.aborted is True
        assert report.items == []
        assert "comparator_client" in report.abort_reason

    def test_cli_with_no_client_available_aborts_exit_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """End-to-end CLI repro of the exact live-run failure: no client
        available (simulated here by monkeypatching build_llm_client to
        return None, exactly what it does with no ANTHROPIC_API_KEY / no
        claude-cli provider), --dry-run NOT passed -- must abort with exit
        1 and a report naming the missing clients, never exit 0 with a
        fabricated agreement_rate."""
        monkeypatch.setattr("athenaeum.provider.build_llm_client", lambda *_a, **_k: None)
        cases_path = EVAL_DATA_ROOT / "detector" / "cases.yaml"
        monkeypatch.chdir(tmp_path)
        exit_code = athenaeum_cli.main(
            ["measure", "shadow-parity", "--cases", str(cases_path), "--json"]
        )
        assert exit_code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["aborted"] is True
        assert payload["items"] == []
        assert "detector_client" in payload["abort_reason"]
        assert "comparator_client" in payload["abort_reason"]


# ---------------------------------------------------------------------------
# AC6 — --max-usd abort, both branches
# ---------------------------------------------------------------------------


class TestMaxUsdAbort:
    def test_preflight_abort_when_projection_exceeds_ceiling(self, tmp_path: Path) -> None:
        cases = [_sized_case("s2", 2)]

        def _exploding_client(*_a: Any, **_k: Any) -> MagicMock:
            client = MagicMock()
            client.messages.create.side_effect = AssertionError("must not be called")
            return client

        report = run_shadow_parity(
            cases,
            detector_client=_exploding_client(),
            comparator_client=_exploding_client(),
            max_usd=1e-12,
            workdir=tmp_path,
        )
        assert report.aborted is True
        assert report.items == []
        assert "exceeds" in report.abort_reason
        assert "--max-usd" in report.abort_reason

    def test_mid_run_abort_returns_partial_report(self, tmp_path: Path) -> None:
        cases = [_sized_case("first", 2), _sized_case("second", 2)]
        # Each call (~100k input / 5k output tokens) costs ~$0.125. With
        # max_usd=0.3: case "first"'s detector+comparator calls land at
        # ~$0.25 (case completes, item appended, post-case check passes);
        # case "second"'s detector call pushes to ~$0.375, so the PER-CALL
        # ceiling guard (QA finding 2) blocks its comparator call before
        # dispatch -- case "second" contributes NO item. This sizing is
        # deliberate: it proves a full case can complete under the ceiling
        # and the NEXT case still aborts mid-case (not merely "eventually"),
        # now that the ceiling is checked between CALLS, not only cases.
        detector_client = _uniform_client(
            _detector_payload(detected=False), input_tokens=100_000, output_tokens=5_000
        )
        comparator_client = _uniform_client(
            _content_relation_payload("compatible"), input_tokens=100_000, output_tokens=5_000
        )

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            max_usd=0.3,
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert len(report.items) == 1
        assert report.items[0].case_id == "first"
        assert len(report.items) < len(cases)
        assert report.usage.estimated_cost_usd > 0.3
        assert "PARTIAL" in render_report(report)
        assert "--max-usd" in report.abort_reason

    def test_abort_fires_mid_cluster_not_only_between_cases(self, tmp_path: Path) -> None:
        """QA finding 2: run_cluster_comparator loops an ENTIRE cluster's
        candidate pairs with no cost hook of its own -- a single oversized
        cluster could otherwise fire every planned pair before a
        between-cases-only ceiling check is ever re-read. Proves the ceiling
        is enforced BETWEEN CALLS: a 4-member cluster plans C(4,2)=6
        comparator pairs, but the run aborts after only 4 of them (a real,
        counted, sub-planned-pair-count number), not after all 6 and not
        merely "eventually" at a case boundary.
        """
        case = _sized_case("big", 4)
        # detector call ~= $0.05, each comparator call ~= $0.10 (see the
        # matching arithmetic in test_mid_run_abort_returns_partial_report's
        # sibling above). max_usd=0.35: detector (0->0.05) then comparator
        # calls 1-4 land exactly on 0.05->0.15->0.25->0.35->0.45; call 5's
        # PRE-DISPATCH check (0.45 > 0.35) blocks it -- 4 of the 6 planned
        # pairs actually fired.
        detector_client = _uniform_client(
            _detector_payload(detected=False), input_tokens=40_000, output_tokens=2_000
        )
        comparator_client = _uniform_client(
            _content_relation_payload("compatible"), input_tokens=80_000, output_tokens=4_000
        )

        report = run_shadow_parity(
            [case],
            detector_client=detector_client,
            comparator_client=comparator_client,
            max_usd=0.35,
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert report.items == []  # the one case never completed
        assert detector_client.messages.create.call_count == 1
        planned = 4 * 3 // 2  # C(4,2) = 6
        assert 0 < comparator_client.messages.create.call_count < planned
        assert comparator_client.messages.create.call_count == 4
        assert "--max-usd" in report.abort_reason
        assert "PARTIAL" in render_report(report)

    def test_post_case_check_still_catches_a_boundary_crossing(self, tmp_path: Path) -> None:
        """The ORIGINAL between-cases check (kept as "belt and braces" per
        QA finding 2) still has a job: a case whose LAST call pushes cost
        over the ceiling completes fully (no further call within that case
        exists for the per-call guard to intercept) -- the post-case check
        is what stops the NEXT case from starting at all.
        """
        cases = [_sized_case("first", 2), _sized_case("second", 2)]
        # Each call ~= $0.125 (see the sibling tests above). max_usd=0.2:
        # case "first"'s detector call (0->0.125) and comparator call
        # (0.125->0.25) both pass their OWN pre-call checks (0 <= 0.2 and
        # 0.125 <= 0.2) -- the per-call guard never fires. Only AFTER the
        # case completes does 0.25 > 0.2 trip the post-case check, before
        # case "second" is even attempted.
        detector_client = _uniform_client(
            _detector_payload(detected=False), input_tokens=100_000, output_tokens=5_000
        )
        comparator_client = _uniform_client(
            _content_relation_payload("compatible"), input_tokens=100_000, output_tokens=5_000
        )

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            max_usd=0.2,
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert len(report.items) == 1
        assert report.items[0].case_id == "first"
        assert "after case" in report.abort_reason  # the post-case message shape
        assert detector_client.messages.create.call_count == 1
        assert comparator_client.messages.create.call_count == 1

    def test_cli_max_usd_abort_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cases_path = EVAL_DATA_ROOT / "detector" / "cases.yaml"
        monkeypatch.chdir(tmp_path)
        exit_code = athenaeum_cli.main(
            [
                "measure",
                "shadow-parity",
                "--cases",
                str(cases_path),
                "--max-usd",
                "1e-12",
            ]
        )
        assert exit_code == 1


# ---------------------------------------------------------------------------
# AC7 — report shape
# ---------------------------------------------------------------------------


class TestReportShape:
    def _sample_report(self, *, aborted: bool = False) -> ParityReport:
        projection = ParityProjection(
            cluster_count=2,
            pairable_cluster_count=2,
            projected_detector_calls=2,
            projected_comparator_calls=2,
            projected_multiplier=1.0,
            projected_cost_usd_lower=0.001,
            projected_cost_usd_upper=0.02,
            detector_model="claude-haiku-test",
            comparator_model="claude-haiku-test",
        )
        matrix = AgreementMatrix()
        matrix.add("factual", "contradiction")
        matrix.add("not-detected", "distinct")
        items = [
            ParityItem(
                case_id="c1",
                source="detector",
                outcome_class="contradict",
                detector_verdict="factual",
                comparator_verdict="contradiction",
                pair_verdicts=[{"a": "a", "b": "b", "verdict": "contradiction"}],
                agreement="agree",
                comparator_correct=True,
                detector_calls=1,
                comparator_calls=1,
            ),
            ParityItem(
                case_id="c2",
                source="detector",
                outcome_class="pass",
                detector_verdict="not-detected",
                comparator_verdict="distinct",
                pair_verdicts=[{"a": "c", "b": "d", "verdict": "distinct"}],
                agreement="agree",
                comparator_correct=True,
                detector_calls=1,
                comparator_calls=1,
            ),
        ]
        from athenaeum.models import TokenUsage

        return ParityReport(
            items=items,
            matrix=matrix,
            detector_calls=2,
            comparator_calls=2,
            call_multiplier=1.0,
            usage=TokenUsage(),
            cost_usd=0.01,
            max_usd=None,
            aborted=aborted,
            abort_reason="observed spend $99.00 exceeds --max-usd $1.00 after case detector/'c2'"
            if aborted
            else "",
            projection=projection,
            athenaeum_version="0.0.0-test",
            git_sha="deadbeef1234",
            generated="2026-01-02T03:04:05Z",
            corpus_digest="abc123def456",
        )

    def test_render_report_contains_required_sections(self) -> None:
        report = self._sample_report()
        text = render_report(report)
        assert "agreement_rate" in text
        assert "1.000" in text  # call_multiplier
        assert "deadbeef1234" in text
        assert "abc123def456" in text
        assert "2026-01-02T03:04:05Z" in text
        assert "0.0.0-test" in text
        assert "PARTIAL" not in text

    def test_render_report_spells_out_agreement_rate_denominator(self) -> None:
        """QA finding 4: the rendered rate must name agree/disagree/
        inconclusive explicitly, not just print a bare number -- a corpus
        that is mostly inconclusive must not read as "the lanes agree on
        90% of the corpus" from the rate alone."""
        report = self._sample_report()  # 2 agree, 0 disagree, 0 inconclusive
        text = render_report(report)
        assert "agree / (agree + disagree)" in text
        assert "INCONCLUSIVE" in text
        assert "2 agree / (2 agree + 0 disagree)" in text
        assert "0 inconclusive item(s) excluded from this rate" in text

    def test_render_report_legends_the_decided_correctly_column(self) -> None:
        report = self._sample_report()
        text = render_report(report)
        assert "decided_correctly` legend" in text
        assert "`True`" in text
        assert "`False`" in text
        assert "`None`" in text

    def test_render_report_partial_banner_when_aborted(self) -> None:
        report = self._sample_report(aborted=True)
        text = render_report(report)
        assert "PARTIAL" in text
        assert "exceeds --max-usd" in text

    def test_write_report_lands_under_out_dir_with_dated_name(self, tmp_path: Path) -> None:
        report = self._sample_report()
        out_dir = tmp_path / "measurements"
        written = write_report(report, out_dir=out_dir)
        assert written == out_dir / "shadow-parity-2026-01-02.md"
        assert written.is_file()
        assert written.read_text(encoding="utf-8") == render_report(report)

    def test_write_report_does_not_clobber_a_same_day_report(self, tmp_path: Path) -> None:
        """QA finding 5: the most likely SECOND same-day run is a retry
        after a --max-usd abort -- overwriting silently would destroy
        exactly the partial artifact the abort path exists to preserve."""
        out_dir = tmp_path / "measurements"
        first_report = self._sample_report()
        first_path = write_report(first_report, out_dir=out_dir)
        assert first_path == out_dir / "shadow-parity-2026-01-02.md"
        first_contents = first_path.read_text(encoding="utf-8")

        second_report = self._sample_report(aborted=True)
        second_path = write_report(second_report, out_dir=out_dir)
        assert second_path == out_dir / "shadow-parity-2026-01-02-2.md"
        assert second_path.is_file()

        # The first file must be untouched -- byte for byte.
        assert first_path.read_text(encoding="utf-8") == first_contents
        assert "PARTIAL" not in first_contents
        assert "PARTIAL" in second_path.read_text(encoding="utf-8")

        # A third same-day write collides with BOTH prior files.
        third_path = write_report(self._sample_report(), out_dir=out_dir)
        assert third_path == out_dir / "shadow-parity-2026-01-02-3.md"

    def test_to_dict_round_trips_json(self) -> None:
        report = self._sample_report()
        payload = report.to_dict()
        json.dumps(payload)  # must not raise
        assert payload["detector_calls"] == 2
        assert payload["matrix"]["total"] == 2


# ---------------------------------------------------------------------------
# AC1 — CLI --help documents --dry-run/--max-usd/output path
# ---------------------------------------------------------------------------


class TestCliHelp:
    def test_help_mentions_dry_run_max_usd_and_output_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            athenaeum_cli.main(["measure", "shadow-parity", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--dry-run" in out
        assert "--max-usd" in out
        assert "measurements" in out

    def test_no_cases_errors_with_exit_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = athenaeum_cli.main(["measure", "shadow-parity"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "athenaeum#1258" in err
