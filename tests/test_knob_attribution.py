# SPDX-License-Identifier: Apache-2.0
"""Cross-module coverage for per-knob spend attribution (issue athenaeum#781).

The seven model knobs (``classify`` / ``write`` / ``resolve`` / ``topic`` /
``reasoning_t1`` / ``reasoning_t2`` / ``rule_proposals``) are defined once in
``prompt_registry._META_ROWS`` (:data:`athenaeum.prompt_registry.KNOBS`). This
file exercises the REAL call site for each knob -- not just
``TokenUsage.add(knob=...)`` directly (that's covered in ``test_models.py``)
-- so a regression in the plumbing between a pipeline function and
``TokenUsage`` shows up here. ``topic`` is exercised through the real ledger
write path in ``tests/test_spend.py::TestQueryTopicsLedger`` instead of here,
because ``query_topics.extract_topics`` records straight to the ledger and
never returns its accumulator to the caller. ``rule_proposals`` (issue
athenaeum#1174) is likewise exercised elsewhere --
``tests/test_librarian_run_phases.py``'s
``TestRunRuleProposalPhase::test_records_usage_and_knob_attribution`` -- since
its real call site is ``librarian._run_rule_proposal_phase``, not a bare
function this module can call standalone.

A trailing drift-guard test pins the knob set actually exercised (here, plus
the two call sites noted above) against :data:`athenaeum.prompt_registry.KNOBS`,
so a new knob added to ``_META_ROWS`` without a corresponding call-site test
fails loudly rather than silently going unattributed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.models import AutoMemoryFile, EntityAction, RawFile, TokenUsage
from athenaeum.prompt_registry import KNOBS


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=response_text)]
    response.usage = MagicMock(input_tokens=111, output_tokens=22)
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# classify -- tier2_classify (src/athenaeum/tiers.py)
# ---------------------------------------------------------------------------


class TestClassifyKnob:
    def test_tier2_classify_tags_classify_knob(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = RawFile(
            path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
            source="sessions",
            timestamp="20240407T120000Z",
            uuid8="aabb0011",
            _content="Some content.",
        )
        client = _mock_client("[]")
        usage = TokenUsage()
        tier2_classify(raw, [], ["person"], [], ["internal"], client, usage=usage)
        assert usage.per_knob["classify"]["input_tokens"] == 111
        assert usage.per_knob["classify"]["output_tokens"] == 22


# ---------------------------------------------------------------------------
# write -- tier3_create (src/athenaeum/tiers.py)
# ---------------------------------------------------------------------------


class TestWriteKnob:
    def test_tier3_create_tags_write_knob(self) -> None:
        from athenaeum.tiers import tier3_create

        action = EntityAction(
            kind="create",
            name="Test Entity",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="Some info.",
        )
        client = _mock_client("# Test Entity\n\nContent.")
        usage = TokenUsage()
        tier3_create(action, "sessions/raw.md", client, usage=usage)
        assert usage.per_knob["write"]["input_tokens"] == 111
        assert usage.per_knob["write"]["output_tokens"] == 22


# ---------------------------------------------------------------------------
# classify -- contradictions.detect_contradictions and
# claim_kind.classify_claim_kind ALSO share the classify knob (both resolve
# via the same "classify" string passed to config.resolve_model).
# ---------------------------------------------------------------------------


def _write_am(scope_dir: Path, filename: str, body: str) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text("---\nname: probe\ntype: feedback\n---\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path, origin_scope="scope-x", memory_type="feedback", name="probe"
    )


class TestContradictionsDetectorSharesClassifyKnob:
    def test_detect_contradictions_tags_classify_knob(self, tmp_path: Path) -> None:
        from athenaeum.contradictions import detect_contradictions

        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "X is true.")
        m2 = _write_am(scope, "b.md", "X is false.")
        client = _mock_client(
            '{"detected": false, "conflict_type": null, '
            '"members_involved": [], "conflicting_passages": [], "rationale": ""}'
        )
        usage = TokenUsage()
        detect_contradictions([m1, m2], client, usage=usage)
        assert usage.per_knob["classify"]["input_tokens"] == 111


class TestClaimKindSharesClassifyKnob:
    def test_classify_claim_kind_tags_classify_knob(self) -> None:
        from athenaeum.claim_kind import classify_claim_kind

        client = _mock_client('{"claim_kind": "fact"}')
        usage = TokenUsage()
        classify_claim_kind("Some memory snippet.", client, usage=usage)
        assert usage.per_knob["classify"]["input_tokens"] == 111


# ---------------------------------------------------------------------------
# resolve -- resolutions.propose_resolution (src/athenaeum/resolutions.py)
# ---------------------------------------------------------------------------


class TestResolveKnob:
    def test_propose_resolution_tags_resolve_knob(self, tmp_path: Path) -> None:
        from athenaeum.contradictions import ContradictionResult
        from athenaeum.resolutions import propose_resolution

        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "Claim A.")
        b = _write_am(scope, "b.md", "Claim B.")
        detected = ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=[f"{a.origin_scope}/{a.path.name}", f"{b.origin_scope}/{b.path.name}"],
            conflicting_passages=["Member A passage.", "Member B passage."],
            rationale="test conflict",
        )
        payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.9, "rationale": "r", "source_precedence_used": []}'
        )
        client = _mock_client(payload)
        usage = TokenUsage()
        propose_resolution(detected, [a, b], client, usage=usage)
        assert usage.per_knob["resolve"]["input_tokens"] == 111


# ---------------------------------------------------------------------------
# reasoning_t1 / reasoning_t2 -- reasoning_tiers.run_t1_tier / run_t2_tier
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, filename: str, *, name: str) -> Path:
    p = tmp_path / filename
    body = " ".join(f"word{i}" for i in range(40))
    p.write_text(f"---\nname: {name}\ntype: reference\n---\n\n{body}\n", encoding="utf-8")
    return p


class TestReasoningT1Knob:
    def test_run_t1_tier_tags_reasoning_t1_knob(self, tmp_path: Path) -> None:
        from athenaeum.reasoning_tiers import ReasoningProposal, run_t1_tier

        src_a = _write_source(tmp_path, "a.md", name="Entity A")
        src_b = _write_source(tmp_path, "b.md", name="Entity B")
        client = _mock_client('{"verdict": "pass_up", "reason": "unsure"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        usage = TokenUsage()
        run_t1_tier(proposal, client=client, usage=usage)
        assert usage.per_knob["reasoning_t1"]["input_tokens"] == 111


class TestReasoningT2Knob:
    def test_run_t2_tier_tags_reasoning_t2_knob(self, tmp_path: Path) -> None:
        from athenaeum.reasoning_tiers import ReasoningProposal, run_t2_tier

        # Same-memory_class, under the page cap, no pii, no axiom -- a safe
        # cluster so the model call is actually reached (see
        # test_t2_reasoning_tier.py::test_safe_class_within_bounds_permits_model_approval).
        src_a = _write_source(tmp_path, "a.md", name="A")
        src_b = _write_source(tmp_path, "b.md", name="B")
        client = _mock_client(
            '{"verdict": "approve", "reason": "safe, homogeneous cluster", '
            '"amended_sources": null, "drafted_body": null}'
        )
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        usage = TokenUsage()
        run_t2_tier(proposal, client=client, usage=usage)
        assert usage.per_knob["reasoning_t2"]["input_tokens"] == 111


# ---------------------------------------------------------------------------
# Drift guard -- the knobs exercised above (plus the two noted elsewhere)
# must equal prompt_registry's derived set (issue athenaeum#781:
# prompt_registry._META_ROWS is the single source of truth; this test fails
# loudly if a knob is added there without a matching call-site test
# somewhere, or vice versa).
# ---------------------------------------------------------------------------


def test_all_registry_knobs_are_exercised_above() -> None:
    exercised = {
        "classify",
        "write",
        "resolve",
        # "topic" is exercised end to end in
        # tests/test_spend.py::TestQueryTopicsLedger::test_records_metered_spend
        "topic",
        "reasoning_t1",
        "reasoning_t2",
        # "rule_proposals" (issue athenaeum#1174) is exercised end to end via
        # librarian._run_rule_proposal_phase in
        # tests/test_librarian_run_phases.py::TestRunRuleProposalPhase::
        # test_records_usage_and_knob_attribution
        "rule_proposals",
    }
    assert exercised == set(KNOBS)
