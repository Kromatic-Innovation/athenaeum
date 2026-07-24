# SPDX-License-Identifier: Apache-2.0
"""Tests for the tiered reasoning-pass pipeline skeleton + T1 tier (#423).

Covers each acceptance criterion named in the issue:

- T1's model payload contains NO full bodies (bounded input scope).
- T1's output type is structurally limited to reject/pass-up — approval is
  unrepresentable (no enum/dataclass variant can express it).
- Every decision writes a machine-readable, queryable reason record.
- With T2 absent, pass-ups land in ``list_pending_decisions`` unchanged.
- Model selection resolves via config (env > yaml > default), never a
  hardcode.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.authority import AuthorityManifest, AuthoritySource
from athenaeum.decisions import list_pending_decisions
from athenaeum.pending_merges import write_pending_merge
from athenaeum.reasoning_tiers import (
    BODY_EXCERPT_WORD_LIMIT,
    DEFAULT_T1_MODEL,
    REASONING_TIER_VERDICTS,
    REJECT_REASON_CROSS_MEMORY_CLASS,
    REJECT_REASON_LIVE_SOURCE_DUPLICATE,
    BoundedSourceView,
    ReasoningProposal,
    ReasoningTierDecision,
    build_bounded_source_view,
    build_t1_request_params,
    default_reasoning_tier_log_path,
    get_t1_model,
    read_reasoning_tier_decisions,
    record_reasoning_tier_decision,
    run_reasoning_pipeline,
    run_t1_tier,
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
    extra_meta: str = "",
) -> Path:
    p = tmp_path / filename
    mclass_line = f"memory_class: {memory_class}\n" if memory_class else ""
    body = " ".join(f"word{i}" for i in range(body_words))
    p.write_text(
        f"---\nname: {name}\ntype: reference\n{mclass_line}{extra_meta}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# AC1 — T1's model payload contains NO full bodies (bounded input scope).
# ---------------------------------------------------------------------------


class TestBoundedInputScope:
    def test_body_excerpt_is_capped_at_word_limit(self, tmp_path: Path) -> None:
        src = _write_source(tmp_path, "a.md", name="Long Page", body_words=500)
        view = build_bounded_source_view(str(src))
        assert len(view.body_excerpt.split()) <= BODY_EXCERPT_WORD_LIMIT
        # The full 500-word body must NOT be present in the excerpt.
        assert "word499" not in view.body_excerpt
        assert "word450" not in view.body_excerpt

    def test_short_body_is_used_in_full_not_padded(self, tmp_path: Path) -> None:
        src = _write_source(tmp_path, "b.md", name="Short Page", body_words=5)
        view = build_bounded_source_view(str(src))
        assert view.body_excerpt.split() == [
            "word0",
            "word1",
            "word2",
            "word3",
            "word4",
        ]

    def test_t1_request_payload_never_contains_full_body(self, tmp_path: Path) -> None:
        # A body long enough that, if leaked whole, would be trivially
        # detectable in the rendered prompt.
        src_a = _write_source(tmp_path, "a.md", name="Entity A", body_words=800)
        src_b = _write_source(tmp_path, "b.md", name="Entity B", body_words=800)
        proposal = ReasoningProposal(
            proposal_id="p1",
            merge_target_name="merged-entity",
            sources=(str(src_a), str(src_b)),
        )
        from athenaeum.reasoning_tiers import bounded_views_for

        views = bounded_views_for(proposal)
        params = build_t1_request_params(proposal, views)
        rendered = json.dumps(params)
        # The tail words of an 800-word body can never appear in a payload
        # bounded to the first ~100 words.
        assert "word799" not in rendered
        assert "word750" not in rendered
        assert "word200" not in rendered
        # Sanity: the excerpt words in-bounds DO appear.
        assert "word0" in rendered

    def test_bounded_source_view_has_no_full_body_attribute(self) -> None:
        # Structural guarantee: BoundedSourceView simply has no field that
        # could carry a full body — only a capped excerpt.
        fields = {f for f in BoundedSourceView.__dataclass_fields__}
        assert fields == {"path", "title", "frontmatter", "body_excerpt"}


# ---------------------------------------------------------------------------
# AC2 — T1 output type structurally limited to reject/pass-up; approval is
# unrepresentable, not merely discouraged.
# ---------------------------------------------------------------------------


class TestApprovalUnrepresentable:
    def test_verdict_literal_has_exactly_two_members(self) -> None:
        assert REASONING_TIER_VERDICTS == {"reject", "pass_up"}
        assert "approve" not in REASONING_TIER_VERDICTS

    def test_decision_construction_rejects_approve_verdict(self) -> None:
        with pytest.raises(ValueError):
            ReasoningTierDecision(
                tier="T1",
                verdict="approve",  # type: ignore[arg-type]
                reason="attempted approval",
                model=None,
                proposal_id="p1",
            )

    def test_decision_construction_rejects_arbitrary_verdict(self) -> None:
        with pytest.raises(ValueError):
            ReasoningTierDecision(
                tier="T1",
                verdict="anything-else",  # type: ignore[arg-type]
                reason="not a real verdict",
                model=None,
                proposal_id="p1",
            )

    def test_model_saying_approve_is_coerced_to_pass_up(self, tmp_path: Path) -> None:
        # Even if the underlying model text claims "approve", T1's parser has
        # no branch that can produce an approval — it degrades to pass_up.
        src_a = _write_source(tmp_path, "a.md", name="Entity A")
        src_b = _write_source(tmp_path, "b.md", name="Entity B")
        client = _mock_client('{"verdict": "approve", "reason": "looks great"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.verdict == "pass_up"
        assert decision.verdict in REASONING_TIER_VERDICTS

    def test_no_code_path_in_module_constructs_an_approval(self) -> None:
        # Exhaustive sweep: every literal string "approve" appearing as a
        # verdict value anywhere reachable from run_t1_tier's source would be
        # a red flag. We assert the source text of the tier functions never
        # assigns verdict="approve" to a ReasoningTierDecision.
        import athenaeum.reasoning_tiers as rt

        src = inspect.getsource(rt)
        # The only appearances of "approve" in the whole module must be in
        # documentation/system-prompt prose, never as a verdict= assignment.
        for line in src.splitlines():
            if 'verdict="approve"' in line or "verdict='approve'" in line:
                pytest.fail(f"found a verdict=approve assignment: {line!r}")

    def test_reason_must_be_nonempty(self) -> None:
        with pytest.raises(ValueError):
            ReasoningTierDecision(
                tier="T1", verdict="pass_up", reason="", model=None, proposal_id="p1"
            )


# ---------------------------------------------------------------------------
# AC3 — every decision writes a machine-readable, queryable reason record.
# ---------------------------------------------------------------------------


class TestDecisionLog:
    def test_round_trip_through_log(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        decision = ReasoningTierDecision(
            tier="T1",
            verdict="reject",
            reason="different entities",
            reason_code="different_entities",
            model="claude-haiku-4-5-20251001",
            proposal_id="abc123",
        )
        ok = record_reasoning_tier_decision(wiki_root, decision)
        assert ok is True

        records = read_reasoning_tier_decisions(wiki_root)
        assert len(records) == 1
        rec = records[0]
        assert rec["tier"] == "T1"
        assert rec["decision"] == "reject"
        assert rec["reason"] == "different entities"
        assert rec["model"] == "claude-haiku-4-5-20251001"
        assert rec["proposal_id"] == "abc123"

    def test_log_is_queryable_by_proposal_id_and_tier(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        d1 = ReasoningTierDecision(
            tier="T1", verdict="reject", reason="r1", model=None, proposal_id="p1"
        )
        d2 = ReasoningTierDecision(
            tier="T1", verdict="pass_up", reason="r2", model=None, proposal_id="p2"
        )
        record_reasoning_tier_decision(wiki_root, d1)
        record_reasoning_tier_decision(wiki_root, d2)

        only_p1 = read_reasoning_tier_decisions(wiki_root, proposal_id="p1")
        assert len(only_p1) == 1
        assert only_p1[0]["proposal_id"] == "p1"

        only_t1 = read_reasoning_tier_decisions(wiki_root, tier="T1")
        assert len(only_t1) == 2

    def test_log_file_is_jsonl_at_default_path(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        decision = ReasoningTierDecision(
            tier="T1", verdict="reject", reason="x", model=None, proposal_id="p1"
        )
        record_reasoning_tier_decision(wiki_root, decision)
        log_path = default_reasoning_tier_log_path(wiki_root)
        assert log_path.exists()
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
        parsed = json.loads(line)  # must be valid JSON per line
        assert parsed["proposal_id"] == "p1"

    def test_reads_empty_list_when_log_missing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert read_reasoning_tier_decisions(wiki_root) == []

    def test_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        decision = ReasoningTierDecision(
            tier="T1", verdict="reject", reason="ok", model=None, proposal_id="p1"
        )
        record_reasoning_tier_decision(wiki_root, decision)
        log_path = default_reasoning_tier_log_path(wiki_root)
        with log_path.open("a", encoding="utf-8") as f:
            f.write('{"v": 1, "ts": "broken')  # torn, no trailing newline/close
        records = read_reasoning_tier_decisions(wiki_root)
        assert len(records) == 1  # torn line skipped, not fatal

    def test_pipeline_records_every_tier_decision(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        src_a = _write_source(tmp_path, "a.md", name="Entity A")
        src_b = _write_source(tmp_path, "b.md", name="Entity B")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )

        def rejecting_handler(p: ReasoningProposal) -> ReasoningTierDecision:
            return ReasoningTierDecision(
                tier="T1", verdict="reject", reason="test reject", model=None,
                proposal_id=p.proposal_id,
            )

        result = run_reasoning_pipeline(
            proposal, tier_chain=(rejecting_handler,), wiki_root=wiki_root
        )
        assert result.rejected is True
        records = read_reasoning_tier_decisions(wiki_root, proposal_id="p1")
        assert len(records) == 1
        assert records[0]["decision"] == "reject"


# ---------------------------------------------------------------------------
# AC4 — with T2 absent, pass-ups land in list_pending_decisions unchanged.
# ---------------------------------------------------------------------------


class TestSkeletonToleratesAbsentT2:
    def test_empty_chain_is_a_passup(self, tmp_path: Path) -> None:
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )
        result = run_reasoning_pipeline(proposal, tier_chain=())
        assert result.rejected is False
        assert result.passed_up is True
        assert result.decisions == ()

    def test_t1_only_chain_passup_flows_to_existing_human_queue_unchanged(
        self, tmp_path: Path
    ) -> None:
        # Simulates the full path: T1 (only tier configured; T2 absent per
        # #432 not yet landing) passes a proposal up, and the proposal is
        # then written to _pending_merges.md exactly as it is today with NO
        # reasoning pipeline in front of it at all -- list_pending_decisions
        # sees it unchanged.
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        src_a = _write_source(tmp_path, "a.md", name="Entity A")
        src_b = _write_source(tmp_path, "b.md", name="Entity B")
        proposal = ReasoningProposal(
            proposal_id="p1",
            merge_target_name="merged-topic",
            sources=(str(src_a), str(src_b)),
        )

        def passing_t1(p: ReasoningProposal) -> ReasoningTierDecision:
            return ReasoningTierDecision(
                tier="T1", verdict="pass_up", reason="cannot confidently reject",
                model="claude-haiku-4-5-20251001", proposal_id=p.proposal_id,
            )

        result = run_reasoning_pipeline(
            proposal, tier_chain=(passing_t1,), wiki_root=wiki_root
        )
        assert result.passed_up is True

        # Caller behavior on a pass-up with T2 absent: write the proposal to
        # the human queue exactly like the pre-#423 call sites do.
        write_pending_merge(
            wiki_root / "_pending_merges.md",
            merge_target_name=proposal.merge_target_name,
            sources=list(proposal.sources),
            rationale="clustered by mechanical layer",
            draft_merged_body="merged body",
            confidence=0.8,
        )

        decisions = list_pending_decisions(wiki_root)
        assert len(decisions) == 1
        assert decisions[0]["type"] == "merge"
        assert decisions[0]["payload"]["merge_target_name"] == "merged-topic"

    def test_rejected_proposal_does_not_reach_pending_merges(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )

        def rejecting_handler(p: ReasoningProposal) -> ReasoningTierDecision:
            return ReasoningTierDecision(
                tier="T1", verdict="reject", reason="different entities",
                reason_code="different_entities", model=None,
                proposal_id=p.proposal_id,
            )

        result = run_reasoning_pipeline(
            proposal, tier_chain=(rejecting_handler,), wiki_root=wiki_root
        )
        assert result.rejected is True
        # Caller contract: a rejected result must never be written to
        # _pending_merges.md. We assert the file was never created (the
        # caller in this test never calls write_pending_merge on reject).
        assert not (wiki_root / "_pending_merges.md").exists()
        decisions = list_pending_decisions(wiki_root)
        assert decisions == []

    def test_chain_is_extensible_for_a_future_t2_handler(self, tmp_path: Path) -> None:
        # A future T2 handler needs only match the TierHandler signature --
        # (ReasoningProposal) -> ReasoningTierDecision -- to slot into the
        # chain after T1, with no change to run_reasoning_pipeline itself.
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )
        calls: list[str] = []

        def t1_pass(p: ReasoningProposal) -> ReasoningTierDecision:
            calls.append("T1")
            return ReasoningTierDecision(
                tier="T1", verdict="pass_up", reason="t1 unsure", model=None,
                proposal_id=p.proposal_id,
            )

        def t2_reject(p: ReasoningProposal) -> ReasoningTierDecision:
            calls.append("T2")
            return ReasoningTierDecision(
                tier="T2", verdict="reject", reason="t2 confident reject",
                model=None, proposal_id=p.proposal_id,
            )

        result = run_reasoning_pipeline(proposal, tier_chain=(t1_pass, t2_reject))
        assert calls == ["T1", "T2"]
        assert result.rejected is True
        assert result.rejecting_decision is not None
        assert result.rejecting_decision.tier == "T2"

    def test_reject_short_circuits_later_tiers(self, tmp_path: Path) -> None:
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )
        calls: list[str] = []

        def t1_reject(p: ReasoningProposal) -> ReasoningTierDecision:
            calls.append("T1")
            return ReasoningTierDecision(
                tier="T1", verdict="reject", reason="t1 reject", model=None,
                proposal_id=p.proposal_id,
            )

        def t2_never_called(p: ReasoningProposal) -> ReasoningTierDecision:
            calls.append("T2")
            return ReasoningTierDecision(
                tier="T2", verdict="pass_up", reason="unreachable", model=None,
                proposal_id=p.proposal_id,
            )

        run_reasoning_pipeline(proposal, tier_chain=(t1_reject, t2_never_called))
        assert calls == ["T1"]  # T2 never invoked once T1 rejected


# ---------------------------------------------------------------------------
# AC5 — model selection resolves via config, not a hardcode.
# ---------------------------------------------------------------------------


class TestModelSelection:
    def test_default_model_used_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_REASONING_T1_MODEL", raising=False)
        assert get_t1_model(None) == DEFAULT_T1_MODEL

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T1_MODEL", "claude-env-override")
        assert get_t1_model(None) == "claude-env-override"

    def test_yaml_config_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_REASONING_T1_MODEL", raising=False)
        config = {"models": {"reasoning_t1": "claude-yaml-override"}}
        assert get_t1_model(config) == "claude-yaml-override"

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T1_MODEL", "claude-env-wins")
        config = {"models": {"reasoning_t1": "claude-yaml-loses"}}
        assert get_t1_model(config) == "claude-env-wins"

    def test_request_params_use_resolved_model_not_hardcode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T1_MODEL", "claude-custom-t1")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )
        params = build_t1_request_params(proposal, ())
        assert params["model"] == "claude-custom-t1"

    def test_run_t1_tier_uses_resolved_model_in_decision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T1_MODEL", "claude-custom-t1")
        src_a = _write_source(tmp_path, "a.md", name="Entity A")
        src_b = _write_source(tmp_path, "b.md", name="Entity B")
        client = _mock_client('{"verdict": "pass_up", "reason": "unsure"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.model == "claude-custom-t1"


# ---------------------------------------------------------------------------
# T1 reject bins: different entities / cross-memory_class / live-source dup.
# ---------------------------------------------------------------------------


class TestT1RejectBins:
    def test_cross_memory_class_rejects_before_any_model_call(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="axiom")
        client = _mock_client('{"verdict": "reject", "reason": "should not be called"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.verdict == "reject"
        assert decision.reason_code == REJECT_REASON_CROSS_MEMORY_CLASS
        client.messages.create.assert_not_called()

    def test_same_memory_class_does_not_reject_on_that_basis(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _mock_client('{"verdict": "pass_up", "reason": "unsure"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.reason_code != REJECT_REASON_CROSS_MEMORY_CLASS

    def test_absent_memory_class_is_tolerated_not_rejected(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A")  # no memory_class
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _mock_client('{"verdict": "pass_up", "reason": "unsure"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.reason_code != REJECT_REASON_CROSS_MEMORY_CLASS

    def test_live_source_duplicate_rejects_before_any_model_call(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(
            tmp_path,
            "a.md",
            name="A",
            extra_meta="topics:\n  - lean-development-workflow\n",
        )
        src_b = _write_source(tmp_path, "b.md", name="B")
        manifest = AuthorityManifest(
            version=1,
            sources=(
                AuthoritySource(
                    slug="skill-dijkstra",
                    location=".claude/skills/dijkstra/SKILL.md",
                    topics=("lean-development-workflow",),
                    kind="skill",
                ),
            ),
        )
        client = _mock_client('{"verdict": "reject", "reason": "should not be called"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client, authority_manifest=manifest)
        assert decision.verdict == "reject"
        assert decision.reason_code == REJECT_REASON_LIVE_SOURCE_DUPLICATE
        client.messages.create.assert_not_called()

    def test_no_manifest_never_rejects_on_duplicate_basis(self, tmp_path: Path) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A")
        src_b = _write_source(tmp_path, "b.md", name="B")
        client = _mock_client('{"verdict": "pass_up", "reason": "unsure"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client, authority_manifest=None)
        assert decision.reason_code != REJECT_REASON_LIVE_SOURCE_DUPLICATE

    def test_different_entities_reject_via_model_call(self, tmp_path: Path) -> None:
        src_a = _write_source(tmp_path, "a.md", name="Apples")
        src_b = _write_source(tmp_path, "b.md", name="Rockets")
        client = _mock_client(
            '{"verdict": "reject", "reason": "different entities: apples vs rockets"}'
        )
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.verdict == "reject"
        client.messages.create.assert_called_once()

    def test_no_client_configured_passes_up(self, tmp_path: Path) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A")
        src_b = _write_source(tmp_path, "b.md", name="B")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=None)
        assert decision.verdict == "pass_up"

    def test_unparseable_model_response_passes_up_not_rejects(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A")
        src_b = _write_source(tmp_path, "b.md", name="B")
        client = _mock_client("not json at all")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t1_tier(proposal, client=client)
        assert decision.verdict == "pass_up"
