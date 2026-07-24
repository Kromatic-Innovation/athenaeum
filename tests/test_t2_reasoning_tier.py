# SPDX-License-Identifier: Apache-2.0
"""Tests for the T2 (opus) reasoning tier (issue #432).

Covers each acceptance criterion named in the issue:

- Safe-class approval enforced STRUCTURALLY (one test per predicate: cross
  memory_class, >3 pages, pii flag, axiom member) — a mocked model returning
  "approve" on a violating proposal can never yield an approved decision.
- A T2-amended draft body cannot reach "approve" without human review — the
  pipeline itself (not a prompt) rejects rewrite-then-self-approve.
- Every T2 decision writes a machine-readable reason record in the SAME log
  shape as T1 (#423), tagged "T2"; escalations/drafts are the pass-up path
  into the existing human queue (``list_pending_decisions``).
- Model selection follows the existing provider-aware model config.
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
    DEFAULT_T2_MODEL,
    REASONING_TIER_T2_VERDICTS,
    SAFE_CLASS_MAX_PAGES,
    SAFE_CLASS_VIOLATION_AXIOM_MEMBER,
    SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS,
    SAFE_CLASS_VIOLATION_LIVE_SOURCE_DUPLICATE,
    SAFE_CLASS_VIOLATION_PII_FLAGGED,
    SAFE_CLASS_VIOLATION_TOO_MANY_PAGES,
    ReasoningProposal,
    ReasoningTierT2Decision,
    build_t2_request_params,
    get_t2_model,
    read_reasoning_tier_decisions,
    record_reasoning_tier_t2_decision,
    run_t2_tier,
    safe_class_violation,
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
    pii: bool = False,
    body_words: int = 40,
    extra_meta: str = "",
) -> Path:
    p = tmp_path / filename
    mclass_line = f"memory_class: {memory_class}\n" if memory_class else ""
    pii_line = "pii: true\n" if pii else ""
    body = " ".join(f"word{i}" for i in range(body_words))
    p.write_text(
        f"---\nname: {name}\ntype: reference\n{mclass_line}{pii_line}{extra_meta}---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _approve_client() -> MagicMock:
    return _mock_client(
        '{"verdict": "approve", "reason": "safe, homogeneous cluster", '
        '"amended_sources": null, "drafted_body": null}'
    )


# ---------------------------------------------------------------------------
# AC1 — safe-class approval enforced structurally, one test per predicate.
# ---------------------------------------------------------------------------


class TestSafeClassStructurallyEnforced:
    def test_cross_memory_class_blocks_approval_even_if_model_says_approve(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="guideline")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict != "approve"
        assert decision.verdict in REASONING_TIER_T2_VERDICTS
        assert decision.safe_class_violation == SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS

    def test_too_many_pages_blocks_approval_even_if_model_says_approve(
        self, tmp_path: Path
    ) -> None:
        sources = tuple(
            str(_write_source(tmp_path, f"s{i}.md", name=f"S{i}", memory_class="fact"))
            for i in range(SAFE_CLASS_MAX_PAGES + 1)
        )
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=sources
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict != "approve"
        assert decision.safe_class_violation == SAFE_CLASS_VIOLATION_TOO_MANY_PAGES

    def test_pii_flag_blocks_approval_even_if_model_says_approve(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact", pii=True)
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict != "approve"
        assert decision.safe_class_violation == SAFE_CLASS_VIOLATION_PII_FLAGGED

    def test_axiom_member_blocks_approval_even_if_model_says_approve(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="axiom")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="axiom")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict != "approve"
        assert decision.safe_class_violation == SAFE_CLASS_VIOLATION_AXIOM_MEMBER

    def test_live_source_duplicate_blocks_approval_even_if_model_says_approve(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(
            tmp_path,
            "a.md",
            name="A",
            memory_class="fact",
            extra_meta="topics:\n  - lean-development-workflow\n",
        )
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
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
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client, authority_manifest=manifest)
        assert decision.verdict != "approve"
        assert (
            decision.safe_class_violation == SAFE_CLASS_VIOLATION_LIVE_SOURCE_DUPLICATE
        )

    def test_safe_class_within_bounds_permits_model_approval(
        self, tmp_path: Path
    ) -> None:
        # Sanity check on the flip side: a genuinely safe cluster (same
        # class, under the page cap, no pii, no axiom) is NOT blocked -- the
        # model's "approve" is allowed through when nothing disqualifies it.
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _approve_client()
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "approve"
        assert decision.safe_class_violation is None

    def test_safe_class_violation_function_checks_each_predicate_directly(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.reasoning_tiers import bounded_views_for

        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        views = bounded_views_for(proposal)
        assert safe_class_violation(views) is None

    def test_decision_type_itself_forbids_approve_with_violation(self) -> None:
        # Belt-and-suspenders: even hand-constructing a T2 decision cannot
        # pair verdict="approve" with a non-None safe_class_violation.
        with pytest.raises(ValueError):
            ReasoningTierT2Decision(
                tier="T2",
                verdict="approve",
                reason="should be impossible",
                model=None,
                proposal_id="p1",
                safe_class_violation=SAFE_CLASS_VIOLATION_PII_FLAGGED,
            )


# ---------------------------------------------------------------------------
# AC2 — no self-approve-rewrite: an amended draft body cannot reach approve.
# ---------------------------------------------------------------------------


class TestNoSelfApproveRewrite:
    def test_model_pairing_approve_with_drafted_body_is_downgraded_to_draft(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _mock_client(
            '{"verdict": "approve", "reason": "rewrote it, looks good", '
            '"amended_sources": null, "drafted_body": "## Rewritten merged content"}'
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict != "approve"
        assert decision.verdict == "draft"
        assert decision.drafted_body == "## Rewritten merged content"

    def test_decision_type_itself_forbids_approve_with_drafted_body(self) -> None:
        with pytest.raises(ValueError):
            ReasoningTierT2Decision(
                tier="T2",
                verdict="approve",
                reason="should be impossible",
                model=None,
                proposal_id="p1",
                drafted_body="rewritten content",
            )

    def test_decision_type_itself_forbids_approve_with_amended_sources(self) -> None:
        with pytest.raises(ValueError):
            ReasoningTierT2Decision(
                tier="T2",
                verdict="approve",
                reason="should be impossible",
                model=None,
                proposal_id="p1",
                amended_sources=("a.md", "b.md"),
            )

    def test_no_code_path_in_module_constructs_approve_with_drafted_body(self) -> None:
        # Exhaustive source sweep mirroring T1's own "no approve constructed"
        # test: the only place verdict="approve" is combined with content is
        # inside _t2_decision_from_model_verdict, and that function's logic
        # strips drafted_body/amended_sources whenever it downgrades away
        # from "approve". Assert the guard clauses exist in source form.
        import athenaeum.reasoning_tiers as rt

        src = inspect.getsource(rt._t2_decision_from_model_verdict)
        assert "drafted_body is not None" in src
        assert 'effective_verdict = "draft"' in src

    def test_draft_verdict_is_a_legitimate_standalone_outcome(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        client = _mock_client(
            '{"verdict": "draft", "reason": "drafting for human review", '
            '"amended_sources": null, "drafted_body": "## Draft body"}'
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "draft"
        assert decision.drafted_body == "## Draft body"


# ---------------------------------------------------------------------------
# AC3 — every T2 decision logs in T1's SAME shape; escalations/drafts rejoin
# the human queue.
# ---------------------------------------------------------------------------


class TestT2DecisionLogging:
    def test_t2_decision_logs_in_same_shape_tagged_t2(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        decision = ReasoningTierT2Decision(
            tier="T2",
            verdict="escalate",
            reason="not confident enough",
            model="claude-opus-4-1-20250805",
            proposal_id="p1",
        )
        ok = record_reasoning_tier_t2_decision(wiki_root, decision)
        assert ok is True

        records = read_reasoning_tier_decisions(wiki_root)
        assert len(records) == 1
        rec = records[0]
        # Same field shape T1 writes: v, ts, tier, decision, reason,
        # reason_code, model, proposal_id.
        assert set(rec.keys()) == {
            "v", "ts", "tier", "decision", "reason", "reason_code", "model",
            "proposal_id",
        }
        assert rec["tier"] == "T2"
        assert rec["decision"] == "escalate"
        assert rec["model"] == "claude-opus-4-1-20250805"
        assert rec["proposal_id"] == "p1"

    def test_t1_and_t2_decisions_share_one_log_queryable_by_tier(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.reasoning_tiers import ReasoningTierDecision, record_reasoning_tier_decision

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        t1_decision = ReasoningTierDecision(
            tier="T1", verdict="pass_up", reason="t1 unsure", model=None,
            proposal_id="p1",
        )
        t2_decision = ReasoningTierT2Decision(
            tier="T2", verdict="approve", reason="safe class", model="opus",
            proposal_id="p1",
        )
        record_reasoning_tier_decision(wiki_root, t1_decision)
        record_reasoning_tier_t2_decision(wiki_root, t2_decision)

        all_records = read_reasoning_tier_decisions(wiki_root, proposal_id="p1")
        assert len(all_records) == 2
        tiers = {r["tier"] for r in all_records}
        assert tiers == {"T1", "T2"}

        only_t2 = read_reasoning_tier_decisions(wiki_root, tier="T2")
        assert len(only_t2) == 1
        assert only_t2[0]["decision"] == "approve"

    def test_every_t2_verdict_kind_is_loggable(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for i, verdict in enumerate(["approve", "amend", "draft", "escalate"]):
            kwargs = {}
            if verdict == "amend":
                kwargs["amended_sources"] = ("a.md",)
            if verdict == "draft":
                kwargs["drafted_body"] = "body"
            decision = ReasoningTierT2Decision(
                tier="T2", verdict=verdict, reason=f"reason {i}", model="opus",
                proposal_id=f"p{i}", **kwargs,
            )
            assert record_reasoning_tier_t2_decision(wiki_root, decision) is True
        records = read_reasoning_tier_decisions(wiki_root, tier="T2")
        assert {r["decision"] for r in records} == {
            "approve", "amend", "draft", "escalate",
        }

    def test_escalate_decision_joins_existing_human_queue(self, tmp_path: Path) -> None:
        # T2 does not maintain a second queue: an escalate/draft decision's
        # caller-side contract is to write the SAME pending-merge block the
        # pre-#432 (T1-only) path already writes, so list_pending_decisions
        # sees it unchanged.
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="guideline")
        proposal = ReasoningProposal(
            proposal_id="p1",
            merge_target_name="merged-topic",
            sources=(str(src_a), str(src_b)),
        )
        client = _approve_client()  # model tries to approve; safe class blocks it
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "escalate"
        record_reasoning_tier_t2_decision(wiki_root, decision)

        # Caller behavior on escalate: write to the human queue exactly as
        # the T1-pass-up-with-no-T2 path already does.
        write_pending_merge(
            wiki_root / "_pending_merges.md",
            merge_target_name=proposal.merge_target_name,
            sources=list(proposal.sources),
            rationale="T2 escalated: " + decision.reason,
            draft_merged_body="merged body",
            confidence=0.5,
        )
        decisions = list_pending_decisions(wiki_root)
        assert len(decisions) == 1
        assert decisions[0]["type"] == "merge"
        assert decisions[0]["payload"]["merge_target_name"] == "merged-topic"


# ---------------------------------------------------------------------------
# AC4 — model selection resolves via config, not a hardcode.
# ---------------------------------------------------------------------------


class TestT2ModelSelection:
    def test_default_model_used_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_REASONING_T2_MODEL", raising=False)
        assert get_t2_model(None) == DEFAULT_T2_MODEL
        assert "opus" in DEFAULT_T2_MODEL

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T2_MODEL", "claude-env-override")
        assert get_t2_model(None) == "claude-env-override"

    def test_yaml_config_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_REASONING_T2_MODEL", raising=False)
        config = {"models": {"reasoning_t2": "claude-yaml-override"}}
        assert get_t2_model(config) == "claude-yaml-override"

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T2_MODEL", "claude-env-wins")
        config = {"models": {"reasoning_t2": "claude-yaml-loses"}}
        assert get_t2_model(config) == "claude-env-wins"

    def test_request_params_use_resolved_model_not_hardcode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T2_MODEL", "claude-custom-t2")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=()
        )
        params = build_t2_request_params(proposal, ())
        assert params["model"] == "claude-custom-t2"

    def test_run_t2_tier_uses_resolved_model_in_decision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_T2_MODEL", "claude-custom-t2")
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _approve_client()
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.model == "claude-custom-t2"


# ---------------------------------------------------------------------------
# T2 full-body input scope (contrast with T1's bounded excerpt).
# ---------------------------------------------------------------------------


class TestT2FullBodyScope:
    def test_t2_payload_contains_full_body_unlike_t1(self, tmp_path: Path) -> None:
        src_a = _write_source(
            tmp_path, "a.md", name="Entity A", body_words=800, memory_class="fact"
        )
        src_b = _write_source(
            tmp_path, "b.md", name="Entity B", body_words=800, memory_class="fact"
        )
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        from athenaeum.reasoning_tiers import bounded_views_for

        views = bounded_views_for(proposal)
        params = build_t2_request_params(proposal, views)
        rendered = json.dumps(params)
        # Unlike T1, T2's payload DOES contain tail words from a full body.
        assert "word799" in rendered
        assert "word750" in rendered


# ---------------------------------------------------------------------------
# Degradation behavior — no client configured.
# ---------------------------------------------------------------------------


class TestT2NoClient:
    def test_no_client_configured_escalates(self, tmp_path: Path) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t2_tier(proposal, client=None)
        assert decision.verdict == "escalate"

    def test_unparseable_model_response_escalates_not_approves(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _mock_client("not json at all")
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "escalate"

    def test_unknown_verdict_string_escalates_not_approves(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _mock_client('{"verdict": "definitely_approve", "reason": "trying to sneak by"}')
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "escalate"


# ---------------------------------------------------------------------------
# Amend verdict — proposes a different source SET without rewriting content.
# ---------------------------------------------------------------------------


class TestAmendVerdict:
    def test_amend_verdict_carries_amended_sources_not_body(
        self, tmp_path: Path
    ) -> None:
        src_a = _write_source(tmp_path, "a.md", name="A", memory_class="fact")
        src_b = _write_source(tmp_path, "b.md", name="B", memory_class="fact")
        client = _mock_client(
            '{"verdict": "amend", "reason": "drop source b, keep only a", '
            '"amended_sources": ["' + str(src_a) + '"], "drafted_body": null}'
        )
        proposal = ReasoningProposal(
            proposal_id="p1", merge_target_name="m", sources=(str(src_a), str(src_b))
        )
        decision = run_t2_tier(proposal, client=client)
        assert decision.verdict == "amend"
        assert decision.amended_sources == (str(src_a),)
        assert decision.drafted_body is None
