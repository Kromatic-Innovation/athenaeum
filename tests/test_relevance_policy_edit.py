# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 7 (athenaeum#2649): conversational policy tool.

Local stdio edits the policy file directly with an audit record; ``--transport
http`` files a proposal that surfaces in the decision queue and, on approval,
applies the same audited edit.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athenaeum.decision_answers import apply_decision_answers, write_decision_answer
from athenaeum.decision_framing import frame_decision
from athenaeum.decisions import list_pending_decisions
from athenaeum.relevance_policy import (
    POLICY_FILENAME,
    RelevancePolicyError,
    load_relevance_policy,
)
from athenaeum.relevance_policy_edit import (
    apply_rule_edit,
    file_rule_edit_proposal,
    list_pending_proposals,
    read_audit,
)

TAGS = "| Tag | U |\n|---|---|\n| active | x |\n| personal | x |\n| personal/travel | x |\n"
TYPES = "| Type | D |\n|---|---|\n| person | x |\n| preference | x |\n"
EXISTING = (
    "# Relevance policy\n\nOperator note kept verbatim.\n\n"
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | | personal | |\n"
)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "tags.md").write_text(TAGS)
    (wiki / "_schema" / "types.md").write_text(TYPES)
    (tmp_path / "raw").mkdir()
    return tmp_path


def _server(kb: Path, **kw):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    return create_server(raw_root=kb / "raw", wiki_root=kb / "wiki", **kw)


def _call(server, name: str, **kwargs):
    async def _run():
        return (await server.get_tool(name)).fn(**kwargs)

    return asyncio.run(_run())


def _policy_file(kb: Path) -> Path:
    return kb / "wiki" / "_schema" / POLICY_FILENAME


class TestLocalEdit:
    def test_tool_changes_policy_file_not_corpus(self, kb: Path) -> None:
        server = _server(kb)  # default transport: stdio
        out = _call(server, "set_capture_rule", verdict="always", tag="personal/travel")
        assert out["ok"] and out["mode"] == "applied" and out["changed"] is True
        # S1 read-back sees it, through both the loader and the MCP read tool.
        policy = load_relevance_policy(kb / "wiki")
        assert policy.resolve(tags=["personal/travel"]) == "always"
        rb = _call(server, "relevance_policy")
        assert [r["verdict"] for r in rb["rules"]] == ["always"]
        # Nothing entered the corpus: no raw intake, no wiki page.
        assert list((kb / "raw").rglob("*")) == []
        assert [p.name for p in (kb / "wiki").glob("*.md")] == []

    def test_appends_after_existing_rules_and_keeps_operator_text(self, kb: Path) -> None:
        _policy_file(kb).write_text(EXISTING)
        apply_rule_edit(kb / "wiki", "always", tag="personal/travel", transport="stdio")
        text = _policy_file(kb).read_text()
        assert "Operator note kept verbatim." in text
        policy = load_relevance_policy(kb / "wiki")
        assert [(r.verdict, r.tag) for r in policy.rules] == [
            ("never", "personal"),
            ("always", "personal/travel"),
        ]
        # last match wins: the new exception overrides the general rule.
        assert policy.resolve(tags=["personal/travel"]) == "always"
        assert policy.resolve(tags=["personal"]) == "never"

    def test_subject_uid_parameter_scopes_the_rule(self, kb: Path) -> None:
        out = _call(_server(kb), "set_capture_rule", verdict="never", subject_uid="ada-example")
        assert out["ok"] and out["rule"]["subject"] == "ada-example"
        policy = load_relevance_policy(kb / "wiki")
        assert policy.resolve(subject="ada-example") == "never"
        assert policy.resolve(subject="someone-else") is None

    def test_invalid_rule_writes_nothing(self, kb: Path) -> None:
        server = _server(kb)
        for kwargs in (
            {"verdict": "maybe", "tag": "personal"},
            {"verdict": "always", "tag": "no-such-tag"},
            {"verdict": "always"},
            {"verdict": "always", "subject_uid": "a|b"},
        ):
            out = _call(server, "set_capture_rule", **kwargs)
            assert out["ok"] is False and out["error_code"] == "invalid_rule"
        assert not _policy_file(kb).exists()
        assert read_audit(kb / "wiki") == []

    def test_refuses_to_edit_an_already_invalid_file(self, kb: Path) -> None:
        _policy_file(kb).write_text(EXISTING.replace("personal", "bogus-tag"))
        with pytest.raises(RelevancePolicyError):
            apply_rule_edit(kb / "wiki", "always", tag="personal", transport="stdio")

    def test_repeating_the_last_rule_is_a_noop(self, kb: Path) -> None:
        apply_rule_edit(kb / "wiki", "always", tag="personal", transport="stdio")
        again = apply_rule_edit(kb / "wiki", "always", tag="personal", transport="stdio")
        assert again.changed is False
        assert len(load_relevance_policy(kb / "wiki").rules) == 1
        assert len(read_audit(kb / "wiki")) == 1

    def test_restricted_caller_is_forbidden(self, kb: Path) -> None:
        server = _server(kb, caller_audience={"team"})
        out = _call(server, "set_capture_rule", verdict="always", tag="personal")
        assert out["ok"] is False and out["error_code"] == "forbidden"
        assert not _policy_file(kb).exists()


class TestAudit:
    def test_direct_edit_appends_audit_record(self, kb: Path) -> None:
        _policy_file(kb).write_text(EXISTING)
        before = load_relevance_policy(kb / "wiki").canonical_hash()[:8]
        _call(_server(kb), "set_capture_rule", verdict="always", tag="personal/travel")
        (rec,) = read_audit(kb / "wiki")
        assert rec["transport"] == "stdio"
        assert rec["at"]
        assert rec["rule"] == {
            "verdict": "always",
            "type": None,
            "tag": "personal/travel",
            "subject": None,
        }
        assert rec["before"]["rules"] == [["never", None, "personal", None]]
        assert rec["before"]["hash"] == before
        assert rec["after"]["rules"] == [
            ["never", None, "personal", None],
            ["always", None, "personal/travel", None],
        ]
        assert rec["after"]["hash"] == load_relevance_policy(kb / "wiki").canonical_hash()[:8]
        assert rec["proposal_id"] is None

    def test_audit_is_jsonl_one_line_per_edit(self, kb: Path) -> None:
        apply_rule_edit(kb / "wiki", "never", tag="personal", transport="stdio")
        apply_rule_edit(kb / "wiki", "always", type_="preference", transport="stdio")
        lines = (kb / "wiki" / "_relevance_policy_audit.jsonl").read_text().splitlines()
        assert [json.loads(x)["rule"]["verdict"] for x in lines] == ["never", "always"]


class TestRemoteProposal:
    def test_http_files_proposal_and_does_not_edit(self, kb: Path) -> None:
        server = _server(kb, transport="http")
        out = _call(
            server,
            "set_capture_rule",
            verdict="always",
            tag="personal/travel",
            rationale="user said so",
        )
        assert out["ok"] and out["mode"] == "proposed" and out["proposal_id"]
        assert not _policy_file(kb).exists()
        assert read_audit(kb / "wiki") == []
        (pending,) = list_pending_proposals(kb / "wiki")
        assert pending["id"] == out["proposal_id"]

    def test_duplicate_pending_proposal_is_not_refiled(self, kb: Path) -> None:
        a, created_a = file_rule_edit_proposal(kb / "wiki", "always", tag="personal")
        b, created_b = file_rule_edit_proposal(kb / "wiki", "always", tag="personal")
        assert created_a and not created_b and a["id"] == b["id"]
        assert len(list_pending_proposals(kb / "wiki")) == 1

    def test_noop_rule_files_no_proposal(self, kb: Path) -> None:
        # Seer review on PR athenaeum#2675: a rule identical to the policy's
        # current last rule changes nothing, so no approval is queued for it.
        apply_rule_edit(kb / "wiki", "always", tag="personal/travel", transport="stdio")
        out = _call(
            _server(kb, transport="http"),
            "set_capture_rule",
            verdict="always",
            tag="personal/travel",
        )
        assert out["ok"] is True and out["mode"] == "unchanged"
        assert list_pending_proposals(kb / "wiki") == []

    def test_invalid_proposal_is_never_queued(self, kb: Path) -> None:
        out = _call(
            _server(kb, transport="http"), "set_capture_rule", verdict="always", tag="nope"
        )
        assert out["ok"] is False and out["error_code"] == "invalid_rule"
        assert list_pending_proposals(kb / "wiki") == []

    def test_proposal_appears_in_decision_queue_framed_and_answerable(self, kb: Path) -> None:
        _call(_server(kb, transport="http"), "set_capture_rule", verdict="never", tag="personal")
        items = [
            d for d in list_pending_decisions(kb / "wiki") if d["type"] == "relevance-policy-edit"
        ]
        assert len(items) == 1
        item = items[0]
        assert item["payload"]["rule"]["tag"] == "personal"
        framed = frame_decision(item, max_context_tokens=1500)
        assert framed["answerable"] is True
        assert framed["reversibility"] == "reversible"
        # Owner-only: a restricted caller never sees it.
        restricted = list_pending_decisions(kb / "wiki", caller_audience={"team"})
        assert all(d["type"] != "relevance-policy-edit" for d in restricted)

    def _answer(self, kb: Path, proposal_id: str, verdict: str) -> None:
        write_decision_answer(
            kb / "raw",
            decision_id=proposal_id,
            decision_type="relevance-policy-edit",
            verdict=verdict,
            wiki_root=kb / "wiki",
        )

    def test_approval_applies_the_edit_with_audit(self, kb: Path) -> None:
        out = _call(
            _server(kb, transport="http"),
            "set_capture_rule",
            verdict="always",
            tag="personal/travel",
        )
        self._answer(kb, out["proposal_id"], "approve")
        report = apply_decision_answers(kb / "wiki", kb / "raw")
        assert [o.applied for o in report.outcomes] == [True], report.outcomes
        assert load_relevance_policy(kb / "wiki").resolve(tags=["personal/travel"]) == "always"
        (rec,) = read_audit(kb / "wiki")
        assert rec["transport"] == "http" and rec["proposal_id"] == out["proposal_id"]
        assert list_pending_proposals(kb / "wiki") == []
        assert all(
            d["type"] != "relevance-policy-edit" for d in list_pending_decisions(kb / "wiki")
        )

    def test_rejection_leaves_policy_unchanged(self, kb: Path) -> None:
        out = _call(
            _server(kb, transport="http"), "set_capture_rule", verdict="never", tag="personal"
        )
        self._answer(kb, out["proposal_id"], "reject")
        report = apply_decision_answers(kb / "wiki", kb / "raw")
        assert [o.applied for o in report.outcomes] == [True]
        assert not _policy_file(kb).exists()
        assert read_audit(kb / "wiki") == []
        assert list_pending_proposals(kb / "wiki") == []

    def test_approval_refused_when_vocabulary_no_longer_allows_rule(self, kb: Path) -> None:
        out = _call(
            _server(kb, transport="http"), "set_capture_rule", verdict="never", tag="personal"
        )
        (kb / "wiki" / "_schema" / "tags.md").write_text("| Tag | U |\n|---|---|\n| active | x |\n")
        self._answer(kb, out["proposal_id"], "approve")
        report = apply_decision_answers(kb / "wiki", kb / "raw")
        assert [o.applied for o in report.outcomes] == [False]
        assert report.outcomes[0].error_code == "invalid_rule"
        assert not _policy_file(kb).exists()
