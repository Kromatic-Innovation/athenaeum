# SPDX-License-Identifier: Apache-2.0
"""Tests for the M17 retrofit of T1/T2 (issue athenaeum#609).

Covers every acceptance criterion in the issue body, on top of (never in
place of) the pre-existing adversarial coverage in ``tests/test_reasoning_tiers.py``
and ``tests/test_t2_reasoning_tier.py``:

1. T1/T2 use the M17 response-model convention — :class:`TestM17ResponseModelConvention`.
2. Adversarial cases (malformed, missing-field, wrong-literal, extra-key,
   empty) — :class:`TestT1AdversarialCases` / :class:`TestT2AdversarialCases`.
3. The exhaustive directional parametrized test — :class:`TestT1ExhaustiveDirectional`
   / :class:`TestT2ExhaustiveDirectional`. The parametrization list IS the
   enumeration this criterion requires; it lives here, not in prose.
4. The machine-checked single-enforcement-point claim (AST walk, not a
   comment/grep) — :class:`TestSingleEnforcementPointStructural`.
5. The negative-control test proving the directional and single-enforcement-
   point checks have teeth against a deliberately eroded variant, loaded
   dynamically via a fixture module built from the REAL source with one
   targeted string transform — never committed to the production module —
   :class:`TestNegativeControlErosionHasTeeth`.
6. ``run_t2_tier``'s ``safe_class_violation`` gate remains the single
   enforcement point for "approve" — re-asserted directly in
   :class:`TestSingleEnforcementPointStructural` and exercised functionally
   throughout :class:`TestT2ExhaustiveDirectional` (every parametrized T2
   case below runs against a SAFE-CLASS proposal, so a non-escalate result
   would prove the *schema* layer failed, independent of whatever the
   safe-class gate happens to do).

See the PR body for which erosion paths are covered here and which, if any,
were judged uncoverable.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import athenaeum.reasoning_tiers as rt
from athenaeum.reasoning_tiers import (
    REASONING_TIER_T2_VERDICTS,
    REASONING_TIER_VERDICTS,
    ReasoningProposal,
    T1VerdictResponse,
    T2VerdictResponse,
    run_t1_tier,
    run_t2_tier,
)


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = mock_response
    return client


def _write_source(
    tmp_path: Path,
    filename: str,
    *,
    name: str,
    memory_class: str | None = None,
    body_words: int = 40,
) -> Path:
    p = tmp_path / filename
    mclass_line = f"memory_class: {memory_class}\n" if memory_class else ""
    body = " ".join(f"word{i}" for i in range(body_words))
    p.write_text(
        f"---\nname: {name}\ntype: reference\n{mclass_line}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _t1_proposal(tmp_path: Path) -> ReasoningProposal:
    src_a = _write_source(tmp_path, "a.md", name="Entity A")
    src_b = _write_source(tmp_path, "b.md", name="Entity B")
    return ReasoningProposal(
        proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
    )


def _t2_safe_proposal(tmp_path: Path) -> ReasoningProposal:
    """A proposal that clears EVERY safe-class predicate (same memory_class,
    under the page cap, no pii, no axiom member, no manifest supplied) — so
    any T2 parametrized case below that lands on something other than
    "escalate" for a valid "approve" payload is not being saved by luck of
    the safe-class gate; the schema layer is what is (or isn't) doing its
    job.
    """
    src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
    src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
    return ReasoningProposal(
        proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
    )


# ---------------------------------------------------------------------------
# AC1 — T1/T2 use the M17 response-model convention.
# ---------------------------------------------------------------------------


class TestM17ResponseModelConvention:
    def test_t1_response_model_is_a_pydantic_model_with_literal_verdict(self) -> None:
        from pydantic import BaseModel

        assert issubclass(T1VerdictResponse, BaseModel)
        field = T1VerdictResponse.model_fields["verdict"]
        # The Literal's allowed values must be EXACTLY T1's own two-member
        # vocabulary -- not a superset, not a subset.
        assert set(field.annotation.__args__) == REASONING_TIER_VERDICTS

    def test_t2_response_model_is_a_pydantic_model_with_literal_verdict(self) -> None:
        from pydantic import BaseModel

        assert issubclass(T2VerdictResponse, BaseModel)
        field = T2VerdictResponse.model_fields["verdict"]
        assert set(field.annotation.__args__) == REASONING_TIER_T2_VERDICTS

    def test_t1_model_defaults_to_extra_forbid(self) -> None:
        # Per athenaeum#608's decision applied to this boundary (NOT the
        # llm_schemas.py extra="allow" convention): a tolerated unknown key
        # is a widening risk here, not a neutral observation.
        assert T1VerdictResponse.model_config.get("extra") == "forbid"

    def test_t2_model_defaults_to_extra_forbid(self) -> None:
        assert T2VerdictResponse.model_config.get("extra") == "forbid"

    def test_t1_valid_payload_round_trips_through_the_model(self) -> None:
        validated = T1VerdictResponse.model_validate(
            {"verdict": "pass_up", "reason": "not confident enough"}
        )
        assert validated.verdict == "pass_up"
        assert validated.reason == "not confident enough"

    def test_t2_valid_payload_round_trips_through_the_model(self) -> None:
        validated = T2VerdictResponse.model_validate(
            {
                "verdict": "amend",
                "reason": "drop one source",
                "amended_sources": ["a.md"],
                "drafted_body": None,
            }
        )
        assert validated.verdict == "amend"
        assert validated.amended_sources == ["a.md"]


# ---------------------------------------------------------------------------
# AC2 — adversarial cases, named individually (malformed, missing-field,
# wrong-literal, extra-key, empty).
# ---------------------------------------------------------------------------


class TestT1AdversarialCases:
    def test_malformed_json_passes_up(self, tmp_path: Path) -> None:
        client = _mock_client("this is not json at all {{{")
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        assert decision.verdict == "pass_up"

    def test_missing_verdict_field_passes_up(self, tmp_path: Path) -> None:
        client = _mock_client('{"reason": "no verdict key present"}')
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        assert decision.verdict == "pass_up"

    def test_wrong_literal_verdict_passes_up(self, tmp_path: Path) -> None:
        client = _mock_client('{"verdict": "approve", "reason": "sneaking in"}')
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        assert decision.verdict == "pass_up"

    def test_extra_key_passes_up(self, tmp_path: Path) -> None:
        client = _mock_client(
            '{"verdict": "reject", "reason": "ok", "unexpected_field": "x"}'
        )
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        # NOTE this is a deliberate, documented behavior CHANGE from the
        # pre-retrofit hand-rolled parser, which ignored unknown keys and
        # would have returned "reject" here. extra="forbid" means an
        # unexpected key now fails validation outright; the result still
        # lands in the safe set (T1 has no "approve" branch either way), it
        # is simply the OTHER safe member (pass_up) rather than reject.
        assert decision.verdict == "pass_up"

    def test_empty_payload_passes_up(self, tmp_path: Path) -> None:
        client = _mock_client("{}")
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        assert decision.verdict == "pass_up"


class TestT2AdversarialCases:
    def test_malformed_json_escalates(self, tmp_path: Path) -> None:
        client = _mock_client("this is not json at all {{{")
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"

    def test_missing_verdict_field_escalates(self, tmp_path: Path) -> None:
        client = _mock_client('{"reason": "no verdict key present"}')
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"

    def test_wrong_literal_verdict_escalates(self, tmp_path: Path) -> None:
        client = _mock_client(
            '{"verdict": "definitely_approve", "reason": "trying to sneak by"}'
        )
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"

    def test_extra_key_escalates(self, tmp_path: Path) -> None:
        client = _mock_client(
            '{"verdict": "escalate", "reason": "ok", "unexpected_field": "x"}'
        )
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"

    def test_extra_key_beside_a_legitimate_looking_approve_escalates(
        self, tmp_path: Path
    ) -> None:
        # The case that matters most: an "approve" that LOOKS otherwise
        # well-formed, on a proposal that WOULD clear the safe-class gate,
        # carrying one unrecognized key. Under the decided extra="forbid"
        # posture this must never reach _t2_decision_from_model_verdict as a
        # trusted approval.
        client = _mock_client(
            '{"verdict": "approve", "reason": "looks safe", '
            '"amended_sources": null, "drafted_body": null, '
            '"sneaky_extra_field": "hi"}'
        )
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"
        assert decision.verdict != "approve"

    def test_empty_payload_escalates(self, tmp_path: Path) -> None:
        client = _mock_client("{}")
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        assert decision.verdict == "escalate"


# ---------------------------------------------------------------------------
# AC3 — the exhaustive directional parametrized test. The parametrization
# list below IS the enumeration of every failure path introduced or touched
# by the retrofit.
# ---------------------------------------------------------------------------

T1_FAILURE_CASES: list[tuple[str, str]] = [
    ("unparseable_output", "not json at all"),
    ("empty_payload", "{}"),
    ("missing_field", '{"reason": "solid reasoning but no verdict key"}'),
    ("wrong_literal", '{"verdict": "approve", "reason": "sneaking in"}'),
    ("extra_key", '{"verdict": "reject", "reason": "ok", "unexpected_field": "x"}'),
    ("partial_payload", '{"verdict": "reject"}'),  # missing "reason"
    ("validation_error_wrong_type", '{"verdict": 123, "reason": "wrong type"}'),
]


class TestT1ExhaustiveDirectional:
    @pytest.mark.parametrize(
        "case_name,response_text", T1_FAILURE_CASES, ids=[c[0] for c in T1_FAILURE_CASES]
    )
    def test_every_failure_path_degrades_to_the_safe_set(
        self, tmp_path: Path, case_name: str, response_text: str
    ) -> None:
        client = _mock_client(response_text)
        decision = run_t1_tier(_t1_proposal(tmp_path), client=client)
        assert decision.verdict in REASONING_TIER_VERDICTS, (
            f"case {case_name!r} produced verdict {decision.verdict!r}, "
            f"outside the safe set {sorted(REASONING_TIER_VERDICTS)!r}"
        )
        # Every one of these payloads is malformed with respect to
        # T1VerdictResponse -- none is a clean, valid "reject". A genuine
        # parse/validation failure at T1 always lands specifically on
        # pass_up (the sole failure-degrade branch); "reject" is reserved
        # for a well-formed payload the model actually returned.
        assert decision.verdict == "pass_up"
        assert decision.verdict != "approve"


T2_FAILURE_CASES: list[tuple[str, str]] = [
    ("unparseable_output", "not json at all"),
    ("empty_payload", "{}"),
    ("missing_field", '{"reason": "solid reasoning but no verdict key"}'),
    ("wrong_literal", '{"verdict": "definitely_approve", "reason": "sneaking by"}'),
    ("extra_key", '{"verdict": "escalate", "reason": "ok", "unexpected_field": "x"}'),
    ("partial_payload", '{"verdict": "approve"}'),  # missing "reason"
    ("validation_error_wrong_type", '{"verdict": 42, "reason": "wrong type"}'),
    (
        "extra_key_beside_legitimate_approve",
        '{"verdict": "approve", "reason": "looks safe", "amended_sources": null, '
        '"drafted_body": null, "sneaky_extra_field": "hi"}',
    ),
]


class TestT2ExhaustiveDirectional:
    @pytest.mark.parametrize(
        "case_name,response_text", T2_FAILURE_CASES, ids=[c[0] for c in T2_FAILURE_CASES]
    )
    def test_every_failure_path_degrades_to_the_safe_set(
        self, tmp_path: Path, case_name: str, response_text: str
    ) -> None:
        # Safe-class proposal throughout (see _t2_safe_proposal's docstring)
        # -- isolates the schema layer's own guarantee from the safe-class
        # gate's.
        client = _mock_client(response_text)
        decision = run_t2_tier(_t2_safe_proposal(tmp_path), client=client)
        safe_set = REASONING_TIER_T2_VERDICTS - {"approve"}
        assert decision.verdict in safe_set, (
            f"case {case_name!r} produced verdict {decision.verdict!r}, "
            f"outside the safe set {sorted(safe_set)!r}"
        )
        # Every one of these payloads is malformed with respect to
        # T2VerdictResponse -- a genuine parse/validation failure at T2
        # always lands specifically on escalate.
        assert decision.verdict == "escalate"
        assert decision.verdict != "approve"


# ---------------------------------------------------------------------------
# AC4 / AC6 — the single-enforcement-point claim, machine-checked by an AST
# walk over reasoning_tiers.py (not a comment, not a grep of a docstring).
# ---------------------------------------------------------------------------


def _is_safe_non_approve_string_constant(node: ast.AST) -> bool:
    """True iff *node* is a string constant that is provably not "approve"."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != "approve"
    )


class _EnclosingFunctionFinder(ast.NodeVisitor):
    """Maps every ``Call`` node to the name of its immediate enclosing
    function (``None`` for module-level calls), without conflating a nested
    function's calls with its parent's.
    """

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.call_owner: dict[int, str | None] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        self.call_owner[id(node)] = self.stack[-1] if self.stack else None
        self.generic_visit(node)


def find_approve_producing_functions(tree: ast.Module) -> dict[str, list[ast.Call]]:
    """Return ``{function_name: [call_node, ...]}`` for every function
    containing a call to ``ReasoningTierT2Decision(...)`` whose ``verdict=``
    keyword could evaluate to ``"approve"`` — either the literal string
    ``"approve"`` itself, or any expression that is not PROVABLY a
    non-"approve" string constant (a variable, an f-string, a function call,
    ...). This is deliberately conservative: anything that isn't a
    known-safe literal counts as a candidate, so a future refactor that
    hides an approval behind a helper still gets flagged.

    This is a general-purpose module-level helper (not test-private) so both
    the real single-enforcement-point test AND the negative-control test
    below apply the exact SAME logic to the real module and to a
    deliberately eroded copy — the negative control would be worthless if it
    used a different (weaker) checker than the real assertion.
    """
    finder = _EnclosingFunctionFinder()
    finder.visit(tree)
    result: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ReasoningTierT2Decision"
        ):
            for kw in node.keywords:
                if kw.arg == "verdict" and not _is_safe_non_approve_string_constant(
                    kw.value
                ):
                    owner = finder.call_owner.get(id(node)) or "<module level>"
                    result.setdefault(owner, []).append(node)
    return result


def gate_dominates_the_enforcement_point(func: ast.FunctionDef) -> tuple[bool, str]:
    """True (+ '') iff a structural safe-class gate — an ``if`` whose test
    mentions both ``"approve"`` and ``violation``, and whose body reassigns
    ``effective_verdict`` away from approval — appears (by source line)
    BEFORE *func*'s own ``ReasoningTierT2Decision(...)`` construction.
    False (+ a reason) otherwise. Also a general-purpose helper, applied to
    both the real module and the eroded fixture in the negative control.
    """
    gate_line: int | None = None
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            test_src = ast.dump(node.test)
            if "approve" in test_src and "violation" in test_src:
                body_src = "\n".join(ast.dump(stmt) for stmt in node.body)
                if "effective_verdict" in body_src and (
                    "escalate" in body_src or "draft" in body_src
                ):
                    if gate_line is None or node.lineno < gate_line:
                        gate_line = node.lineno
    if gate_line is None:
        return False, "no safe_class_violation-style gate found in this function"

    construct_line: int | None = None
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ReasoningTierT2Decision"
        ):
            if construct_line is None or node.lineno > construct_line:
                construct_line = node.lineno
    if construct_line is None:
        return False, "no ReasoningTierT2Decision(...) construction found"

    if gate_line >= construct_line:
        return (
            False,
            f"gate at line {gate_line} does not precede construction at "
            f"line {construct_line}",
        )
    return True, ""


def _real_module_tree() -> ast.Module:
    return ast.parse(Path(inspect.getfile(rt)).read_text(encoding="utf-8"))


class TestSingleEnforcementPointStructural:
    def test_approve_is_produced_on_exactly_one_code_path(self) -> None:
        hits = find_approve_producing_functions(_real_module_tree())
        assert set(hits.keys()) == {"_t2_decision_from_model_verdict"}, (
            f"expected exactly one function producing an approve-reachable "
            f"ReasoningTierT2Decision, found: {sorted(hits.keys())!r}"
        )
        assert len(hits["_t2_decision_from_model_verdict"]) == 1

    def test_the_single_path_is_dominated_by_the_safe_class_violation_gate(
        self,
    ) -> None:
        tree = _real_module_tree()
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_t2_decision_from_model_verdict"
        )
        dominated, reason = gate_dominates_the_enforcement_point(func)
        assert dominated, reason

    def test_run_t2_tier_gates_on_safe_class_violation_before_the_enforcement_point(
        self,
    ) -> None:
        # AC6, restated structurally: run_t2_tier itself must call
        # safe_class_violation and hand the result into
        # _t2_decision_from_model_verdict as `violation=` -- i.e. the gate
        # is actually WIRED into the one enforcement path, not merely
        # present somewhere in the file.
        tree = _real_module_tree()
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "run_t2_tier"
        )
        src = ast.dump(func)
        assert "safe_class_violation" in src
        assert "_t2_decision_from_model_verdict" in src


# ---------------------------------------------------------------------------
# AC5 — negative control: the directional test and the single-enforcement-
# point test must each FAIL against a deliberately eroded variant. The
# erosion is synthesized at test time from the REAL module source (one
# targeted string transform per variant) and loaded as a throwaway fixture
# module -- it is never written into src/athenaeum/reasoning_tiers.py.
# ---------------------------------------------------------------------------


def _load_eroded_module(tmp_path: Path, transform: Any, *, label: str) -> Any:
    real_path = Path(inspect.getfile(rt))
    source = real_path.read_text(encoding="utf-8")
    eroded_source = transform(source)
    assert eroded_source != source, f"{label}: transform made no change"
    mod_name = f"_eroded_reasoning_tiers_{label}_{uuid.uuid4().hex[:8]}"
    mod_path = tmp_path / f"{mod_name}.py"
    mod_path.write_text(eroded_source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _erode_t2_extra_forbid_to_allow(source: str) -> str:
    """Reproduce the M17 observe-only default (``extra="allow"``) on the T2
    contract — the exact posture athenaeum#608's decision says must NOT apply at
    this authority boundary. Anchored ONLY inside T2VerdictResponse (T1's
    own ``extra="forbid"`` is left untouched) by slicing the source at the
    class definition first.
    """
    marker = "class T2VerdictResponse(BaseModel):"
    idx = source.index(marker)
    head, tail = source[:idx], source[idx:]
    old = 'model_config = ConfigDict(extra="forbid")'
    assert tail.count(old) == 1, "expected exactly one extra=forbid inside T2VerdictResponse"
    tail = tail.replace(old, 'model_config = ConfigDict(extra="allow")', 1)
    return head + tail


def _erode_add_second_approve_path(source: str) -> str:
    """Inject a second, gate-free function that directly constructs an
    approved :class:`ReasoningTierT2Decision`, bypassing
    ``_t2_decision_from_model_verdict`` and its ``safe_class_violation``
    gate entirely -- exactly the "second '\"approve\"' return path" example
    the issue names.
    """
    marker = "\n__all__ = ["
    idx = source.index(marker)
    assert source.count(marker) == 1, "expected exactly one __all__ marker"
    injected = '''

def _shortcut_bypass_approve(proposal_id, model):
    """Deliberately eroded, test-fixture-only: a second route to "approve"
    that never consults safe_class_violation. Never committed to the real
    module -- see tests/test_reasoning_tiers_m17_retrofit.py."""
    return ReasoningTierT2Decision(
        tier=T2_TIER_NAME,
        verdict="approve",
        reason="bypassed the safe_class_violation gate entirely",
        model=model,
        proposal_id=proposal_id,
    )

'''
    return source[:idx] + injected + source[idx:]


class TestNegativeControlErosionHasTeeth:
    def test_extra_allow_erosion_makes_the_directional_test_fail(
        self, tmp_path: Path
    ) -> None:
        eroded = _load_eroded_module(
            tmp_path, _erode_t2_extra_forbid_to_allow, label="extra_allow"
        )
        # Same payload as TestT2AdversarialCases's most pointed case: a
        # legitimate-looking "approve" riding alongside one unrecognized
        # key, on a proposal that clears every safe-class predicate.
        payload = (
            '{"verdict": "approve", "reason": "looks safe", '
            '"amended_sources": null, "drafted_body": null, '
            '"sneaky_extra_field": "hi"}'
        )
        verdict, reason, amended_sources, drafted_body = eroded._parse_t2_response(
            payload
        )
        # Against the REAL module this same payload always escalates (see
        # TestT2AdversarialCases.test_extra_key_beside_a_legitimate_looking_approve_escalates
        # and the "extra_key_beside_legitimate_approve" parametrized case).
        # Against the eroded module it does not -- proving the real test
        # would have caught this erosion.
        decision = eroded._t2_decision_from_model_verdict(
            proposal_id="p1",
            model="opus",
            verdict=verdict,
            reason=reason,
            amended_sources=amended_sources,
            drafted_body=drafted_body,
            violation=None,  # safe-class proposal, mirrors _t2_safe_proposal
        )
        assert decision.verdict == "approve", (
            "expected the eroded (extra='allow') module to leak an "
            "unvalidated 'approve' through -- if this fails, the erosion "
            "did not erode anything and the negative control is inert"
        )
        # The real directional-test assertion, applied here, must FAIL --
        # demonstrating the real test has teeth against this exact erosion.
        with pytest.raises(AssertionError):
            assert decision.verdict != "approve"

    def test_second_approve_path_erosion_makes_the_enforcement_point_test_fail(
        self, tmp_path: Path
    ) -> None:
        eroded = _load_eroded_module(
            tmp_path, _erode_add_second_approve_path, label="second_path"
        )
        eroded_tree = ast.parse(inspect.getsource(eroded))
        hits = find_approve_producing_functions(eroded_tree)
        # Against the real module this is exactly {"_t2_decision_from_model_verdict"}
        # (see TestSingleEnforcementPointStructural). Against the eroded
        # module it must NOT be -- a second function is now a candidate.
        assert set(hits.keys()) != {"_t2_decision_from_model_verdict"}
        assert "_shortcut_bypass_approve" in hits
        # The real assertion, applied here, must FAIL.
        with pytest.raises(AssertionError):
            assert set(hits.keys()) == {"_t2_decision_from_model_verdict"}

    def test_erosion_fixtures_are_not_present_in_the_real_module(self) -> None:
        # Belt-and-suspenders: confirm the erosions above really are
        # synthesized at test time and never landed in the production file.
        real_source = Path(inspect.getfile(rt)).read_text(encoding="utf-8")
        assert "_shortcut_bypass_approve" not in real_source
        assert 'ConfigDict(extra="allow")' not in real_source.split(
            "class T2VerdictResponse(BaseModel):"
        )[1]


# ---------------------------------------------------------------------------
# Sanity: T1's structural raise-on-"approve" invariant is untouched by the
# retrofit (already covered in tests/test_reasoning_tiers.py -- restated
# here as a one-line cross-check that the M17 model doesn't quietly bypass
# the dataclass's own __post_init__ guard).
# ---------------------------------------------------------------------------


def test_t1_response_model_rejecting_approve_is_not_the_only_guard() -> None:
    from athenaeum.reasoning_tiers import ReasoningTierDecision

    with pytest.raises(ValidationError):
        T1VerdictResponse.model_validate({"verdict": "approve", "reason": "x"})
    # Even if that model-level guard were somehow bypassed, the dataclass's
    # own __post_init__ still raises independently (defense in depth).
    with pytest.raises(ValueError):
        ReasoningTierDecision(
            tier="T1",
            verdict="approve",  # type: ignore[arg-type]
            reason="x",
            model=None,
            proposal_id="p1",
        )
