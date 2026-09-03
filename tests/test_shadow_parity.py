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
# detector_verdict_from_result
# ---------------------------------------------------------------------------


class TestDetectorVerdictFromResult:
    def test_incomplete_is_unavailable_even_when_detected(self) -> None:
        result = ContradictionResult(detected=True, conflict_type="factual", incomplete=True)
        assert detector_verdict_from_result(result) == "unavailable"

    def test_not_detected(self) -> None:
        result = ContradictionResult(detected=False, rationale="llm-unavailable")
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
        # comparator_calls counts PAIRS run through the comparator (the same
        # "comparator call" vocabulary athenaeum.cluster_comparator.planned_pair_count
        # already uses -- gate on or off, LLM spent or not), so the one
        # Gate-1-resolved pair still counts as 1 here even though it cost
        # zero tokens; the client-invocation count below is the separate,
        # LLM-spend-specific fact.
        assert by_id["case_a"].comparator_calls == 1
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
        # Large token counts so the FIRST case alone blows a tiny ceiling,
        # while the small synthetic prompts keep the pre-run PROJECTION
        # well under it (so this exercises the mid-run branch, not the
        # pre-run one).
        detector_client = _uniform_client(
            _detector_payload(detected=False), input_tokens=2_000_000, output_tokens=100_000
        )
        comparator_client = _uniform_client(
            _content_relation_payload("compatible"), input_tokens=2_000_000, output_tokens=100_000
        )

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            max_usd=0.01,
            workdir=tmp_path,
        )

        assert report.aborted is True
        assert len(report.items) >= 1
        assert len(report.items) < len(cases)
        assert report.usage.estimated_cost_usd > 0.01
        assert "PARTIAL" in render_report(report)

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
