# SPDX-License-Identifier: Apache-2.0
"""Answer record v2: who / where / what was shown (issue athenaeum#2530).

Slice S3 of design athenaeum#2489 (section 4, ruling D2). Covers, per the
issue's acceptance criteria:

1. Every write path records the ``answer_version: 2`` fields.
2. An answer naming a fingerprint the item no longer has ends ``stale``: not
   applied, and the item is still offered, with its new fingerprint.
3. An answer with no fingerprint applies as ``unverified`` on a
   competence-routed item and is held on an authority-routed one.
4. An agent's answer on an authority-routed item is refused.
5. The budget-ledger answer event carries ``channel.host`` and
   ``evidence.strength``.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import decision_answers as da
from athenaeum.cli import main as cli_main
from athenaeum.decision_answers import (
    ANSWER_VERSION,
    AnswerRecordInvalid,
    MalformedDecisionAnswer,
    apply_decision_answers,
    current_fingerprints,
    write_and_apply_decision_answer,
    write_decision_answer,
)
from athenaeum.decision_budget import DECISION_BUDGET_EVENTS_VERSION, read_decision_budget_events
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import parse_frontmatter
from tests.test_decision_answers import (
    _seed_audit_item,
    _seed_rule_proposal,
    _write_merge,
    _write_question_block,
)

_STALE_FP = "sha256:" + "0" * 64


@pytest.fixture
def kroot(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw").mkdir()
    return tmp_path


def _wiki(kroot: Path) -> Path:
    return kroot / "wiki"


def _question(kroot: Path, **kw: Any) -> str:
    return _write_question_block(_wiki(kroot) / "_pending_questions.md", **kw)


def _item(kroot: Path, decision_id: str) -> dict[str, Any] | None:
    return next((d for d in list_pending_decisions(_wiki(kroot)) if d["id"] == decision_id), None)


def _fp(kroot: Path, decision_id: str) -> str:
    item = _item(kroot, decision_id)
    assert item is not None
    return str(item["fingerprint"])


def _meta(path: Path) -> dict[str, Any]:
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    return meta


def _answer(kroot: Path, decision_id: str, decision_type: str, verdict: str, **kw: Any):
    return write_and_apply_decision_answer(
        kroot / "raw",
        _wiki(kroot),
        decision_id=decision_id,
        decision_type=decision_type,
        verdict=verdict,
        **kw,
    )


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _cli_answer(kroot: Path, qid: str, dtype: str, verdict: str, *extra: str):
    return _run(
        [
            "decisions", "answer", "--path", str(kroot), "--id", qid, "--type", dtype,
            "--answer", json.dumps({"verdict": verdict}), "--json", *extra,
        ]
    )  # fmt: skip


def _mcp(kroot: Path, name: str):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    server = create_server(raw_root=kroot / "raw", wiki_root=_wiki(kroot))

    async def _get():
        return (await server.get_tool(name)).fn

    return asyncio.run(_get())


def _confirmation(kroot: Path) -> str:
    """Raise a ``confirmation`` (authority-routed, answered through the
    ``question`` applier) the way an agent does."""
    from tests.test_raise_decision import _CONFIRMATION_KWARGS

    raised = _mcp(kroot, "raise_decision")(kind="confirmation", **_CONFIRMATION_KWARGS)
    assert raised["ok"] is True
    return str(raised["decision_id"])


# ---------------------------------------------------------------------------
# AC1: every write path accepts and records the answer_version 2 fields
# ---------------------------------------------------------------------------


class TestRecordFields:
    def test_writer_records_every_field_and_the_loader_reads_them_back(
        self, kroot: Path
    ) -> None:
        qid = _question(kroot)
        fp = _fp(kroot, qid)
        path = write_decision_answer(
            kroot / "raw",
            decision_id=qid,
            decision_type="question",
            verdict="Series B.",
            shown_fingerprint=fp,
            answerer={"id": "ana", "kind": "human"},
            channel={"host": "claude-code", "surface": "question-ui", "adapter": "skill"},
            evidence={"strength": "asserted", "ref": "chat-42"},
        )
        meta = _meta(path)
        assert meta["answer_version"] == ANSWER_VERSION == 2
        assert meta["shown_fingerprint"] == fp
        assert meta["answerer"] == {"id": "ana", "kind": "human"}
        assert meta["channel"] == {
            "host": "claude-code",
            "surface": "question-ui",
            "adapter": "skill",
        }
        assert meta["evidence"] == {"strength": "asserted", "ref": "chat-42"}

        parsed = da._load_decision_answer(path)
        assert parsed is not None
        assert parsed.answer_version == 2
        assert parsed.shown_fingerprint == fp
        assert parsed.answerer_kind == "human"
        assert parsed.channel_host == "claude-code"
        assert parsed.evidence_strength == "asserted"
        # athenaeum#2523's attribution: a human answerer is the actor.
        assert parsed.actor == "ana"

    def test_no_fields_keeps_the_version_1_record(self, kroot: Path) -> None:
        path = write_decision_answer(
            kroot / "raw", decision_id=_question(kroot), decision_type="question", verdict="x"
        )
        meta = _meta(path)
        for key in ("answer_version", "shown_fingerprint", "answerer", "channel", "evidence"):
            assert key not in meta
        parsed = da._load_decision_answer(path)
        assert parsed is not None and parsed.answer_version == 1

    def test_cli_records_the_fields_and_defaults_its_channel(self, kroot: Path) -> None:
        qid = _question(kroot)
        fp = _fp(kroot, qid)
        rc, out, _ = _cli_answer(
            kroot, qid, "question", "Series B.",
            "--shown-fingerprint", fp, "--answerer-id", "ana", "--answerer-kind", "human",
            "--evidence-strength", "asserted",
        )  # fmt: skip
        payload = json.loads(out)
        assert rc == 0 and payload["applied"] is True
        meta = _meta(Path(payload["answer_file"]))
        assert meta["answer_version"] == 2
        assert meta["shown_fingerprint"] == fp
        assert meta["answerer"] == {"id": "ana", "kind": "human"}
        assert meta["channel"]["host"] == "cli"
        assert meta["channel"]["surface"] == "tty"
        assert meta["channel"]["adapter"] == "athenaeum-cli"
        assert meta["channel"]["adapter_version"]
        assert meta["evidence"] == {"strength": "asserted", "ref": None}
        # A named answerer replaces the athenaeum#2523 placeholder.
        assert "actor" not in meta

    def test_cli_channel_flags_override_the_default(self, kroot: Path) -> None:
        rc, out, _ = _cli_answer(
            kroot, _question(kroot), "question", "Series B.",
            "--channel-host", "grok", "--channel-surface", "chat",
        )  # fmt: skip
        assert rc == 0
        meta = _meta(Path(json.loads(out)["answer_file"]))
        assert meta["channel"]["host"] == "grok"
        assert meta["channel"]["surface"] == "chat"
        # No answerer named: the placeholder actor is still recorded.
        assert meta["actor"] == "unattributed"

    def test_an_answerer_of_unknown_kind_keeps_the_human_placeholder(
        self, kroot: Path
    ) -> None:
        # Seer finding on PR athenaeum#3150: naming an answerer without a kind
        # must not turn a human surface's merge stamp into a pipeline one.
        wiki = _wiki(kroot)
        mid = _write_merge(
            wiki / "_pending_merges.md", target="Gamma", src_a=wiki / "a.md", src_b=wiki / "b.md"
        )
        rc, out, _ = _cli_answer(
            kroot, mid, "merge", "reject", "--apply", "deferred", "--answerer-id", "ana"
        )
        assert rc == 0
        path = Path(json.loads(out)["answer_file"])
        assert _meta(path)["actor"] == "unattributed"
        parsed = da._load_decision_answer(path)
        assert parsed is not None and parsed.answerer_kind == "unknown"
        assert da._decided_by(parsed, "pipeline:merge-reject") == "human:unattributed"

        res = _mcp(kroot, "resolve_merge")(
            mid, "reject", apply="deferred", answerer={"id": "ana"}
        )
        assert _meta(Path(res["answer_file"]))["actor"] == "unattributed"

    def test_mcp_resolve_question_records_the_fields(self, kroot: Path) -> None:
        qid = _question(kroot)
        fp = _fp(kroot, qid)
        res = _mcp(kroot, "resolve_question")(
            qid,
            "Series B.",
            shown_fingerprint=fp,
            answerer={"id": "ana", "kind": "human"},
            channel={"host": "claude-code", "surface": "question-ui"},
        )
        assert res["ok"] is True and res["applied"] is True
        meta = _meta(Path(res["answer_file"]))
        assert meta["answer_version"] == 2
        assert meta["shown_fingerprint"] == fp
        assert meta["answerer"] == {"id": "ana", "kind": "human"}
        assert meta["channel"]["host"] == "claude-code"
        assert meta["channel"]["surface"] == "question-ui"
        # Keys the caller left out are what the server itself knows.
        assert meta["channel"]["adapter"] == "athenaeum-mcp"

    def test_mcp_resolve_merge_records_the_fields(self, kroot: Path) -> None:
        wiki = _wiki(kroot)
        mid = _write_merge(
            wiki / "_pending_merges.md",
            target="Gamma",
            src_a=wiki / "a.md",
            src_b=wiki / "b.md",
        )
        fp = _fp(kroot, mid)
        res = _mcp(kroot, "resolve_merge")(
            mid,
            "reject",
            apply="deferred",
            shown_fingerprint=fp,
            answerer={"id": "ana", "kind": "human"},
        )
        assert res["ok"] is True
        meta = _meta(Path(res["answer_file"]))
        assert meta["shown_fingerprint"] == fp
        assert meta["answerer"] == {"id": "ana", "kind": "human"}
        assert meta["channel"] == {
            "host": "unknown",
            "surface": "mcp",
            "adapter": "athenaeum-mcp",
            "adapter_version": meta["channel"]["adapter_version"],
        }
        assert "actor" not in meta

    def test_mcp_review_audit_item_records_the_fields(
        self, kroot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.config as config_mod

        monkeypatch.setattr(config_mod, "resolve_reasoning_tier_any_screen_enabled", lambda c: True)
        aid = _seed_audit_item(_wiki(kroot), pid="p-1")
        fp = _fp(kroot, aid)
        res = _mcp(kroot, "review_audit_item")(
            aid,
            "approve",
            apply="deferred",
            shown_fingerprint=fp,
            evidence={"strength": "unverified", "ref": None},
        )
        assert res["ok"] is True
        meta = _meta(Path(res["answer_file"]))
        assert meta["shown_fingerprint"] == fp
        assert meta["evidence"] == {"strength": "unverified", "ref": None}

    def test_mcp_refuses_a_bad_field_before_writing(self, kroot: Path) -> None:
        res = _mcp(kroot, "resolve_question")(
            _question(kroot), "Series B.", answerer={"id": "ana", "kind": "robot"}
        )
        assert res["ok"] is False
        assert res["error_code"] == "invalid_answer_record"
        assert not list((kroot / "raw").glob("answers/*.md"))

    def test_cli_refuses_a_bad_field_before_writing(self, kroot: Path) -> None:
        rc, _out, err = _cli_answer(
            kroot, _question(kroot), "question", "x", "--shown-fingerprint", "not-a-hash"
        )
        assert rc != 0
        # A refusal is reported on stderr, like every other answer refusal.
        assert json.loads(err)["error_code"] == "invalid_answer_record"
        assert not list((kroot / "raw").glob("answers/*.md"))

    def test_triage_records_itself_as_an_agent_with_the_item_fingerprint(
        self, kroot: Path
    ) -> None:
        from athenaeum.triage import TRIAGE_ANSWERER_ID, submit_answer

        qid = _question(kroot)
        fp = _fp(kroot, qid)
        sub = submit_answer(
            kroot,
            decision_id=qid,
            decision_type="question",
            answer={"verdict": "Series B."},
            shown_fingerprint=fp,
        )
        assert sub.ok and sub.path is not None
        meta = _meta(sub.path)
        assert meta["shown_fingerprint"] == fp
        assert meta["answerer"] == {"id": TRIAGE_ANSWERER_ID, "kind": "agent"}
        assert meta["channel"]["host"] == "athenaeum"
        assert meta["channel"]["surface"] == "triage"

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"shown_fingerprint": "sha256:abc"}, "shown_fingerprint"),
            ({"answerer": {"kind": "human"}}, "answerer.id"),
            ({"answerer": {"id": "a", "kind": "robot"}}, "answerer.kind"),
            ({"answerer": {"id": "a", "role": "x"}}, "unknown key"),
            ({"channel": {"surface": "tty"}}, "channel.host"),
            ({"channel": {"host": "cli", "colour": "x"}}, "unknown key"),
            ({"evidence": {"strength": "strong"}}, "evidence.strength"),
            ({"evidence": {"strength": "verified"}}, "needs a shown_fingerprint"),
        ],
    )
    def test_writer_refuses_fields_that_do_not_fit(
        self, kroot: Path, kwargs: dict[str, Any], fragment: str
    ) -> None:
        with pytest.raises(AnswerRecordInvalid, match=fragment):
            write_decision_answer(
                kroot / "raw", decision_id="q", decision_type="question", verdict="x", **kwargs
            )
        assert not list((kroot / "raw").glob("answers/*.md"))

    def test_a_version_2_file_without_a_fingerprint_is_malformed(self, kroot: Path) -> None:
        answers = kroot / "raw" / "answers"
        answers.mkdir()
        path = answers / "hand.md"
        meta = {
            "source": "decision_answer",
            "decision_id": "q1",
            "decision_type": "question",
            "verdict": "x",
            "answer_version": 2,
        }
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n", encoding="utf-8")
        with pytest.raises(MalformedDecisionAnswer, match="requires shown_fingerprint"):
            da._load_decision_answer(path)


# ---------------------------------------------------------------------------
# AC2: a stale fingerprint is not applied, and the item is offered again
# ---------------------------------------------------------------------------


class TestStale:
    def test_an_item_that_changed_after_it_was_shown_refuses_the_answer(
        self, kroot: Path
    ) -> None:
        pending = _wiki(kroot) / "_pending_questions.md"
        qid = _question(kroot)
        shown = _fp(kroot, qid)

        # The item changes after it was shown: same id, new description.
        pending.write_text("", encoding="utf-8")
        assert _question(kroot, description="New raw now says Series C.") == qid
        now = _fp(kroot, qid)
        assert now != shown

        result = _answer(kroot, qid, "question", "Series B.", shown_fingerprint=shown)
        assert result.status == "refused"
        assert result.error_code == "stale"
        assert result.outcome is not None and result.outcome.applied is False
        # Not applied ...
        assert "- [ ] " in pending.read_text(encoding="utf-8")
        # ... and offered again, with its current framing.
        again = _item(kroot, qid)
        assert again is not None and again["fingerprint"] == now

    def test_the_deferred_tick_refuses_it_too(self, kroot: Path) -> None:
        qid = _question(kroot)
        write_decision_answer(
            kroot / "raw",
            decision_id=qid,
            decision_type="question",
            verdict="x",
            shown_fingerprint=_STALE_FP,
        )
        report = apply_decision_answers(_wiki(kroot), kroot / "raw")
        assert report.applied == 0
        assert [o.error_code for o in report.outcomes] == ["stale"]
        assert _item(kroot, qid) is not None

    def test_the_current_fingerprint_applies(self, kroot: Path) -> None:
        qid = _question(kroot)
        result = _answer(kroot, qid, "question", "x", shown_fingerprint=_fp(kroot, qid))
        assert result.status == "applied"
        assert _item(kroot, qid) is None

    def test_a_fingerprint_listed_with_proposals_is_current_too(self, kroot: Path) -> None:
        qid = _question(kroot)
        with_proposal = next(
            d for d in list_pending_decisions(_wiki(kroot), with_proposal=True) if d["id"] == qid
        )
        assert with_proposal["fingerprint"] in current_fingerprints(_wiki(kroot), "question", qid)
        result = _answer(
            kroot, qid, "question", "x", shown_fingerprint=with_proposal["fingerprint"]
        )
        assert result.status == "applied"

    def test_a_resolved_item_keeps_its_own_error_code(self, kroot: Path) -> None:
        # The preflight runs only on a pending item: an already-answered one
        # is still reported the way the applier always has.
        qid = _question(kroot)
        fp = _fp(kroot, qid)
        assert _answer(kroot, qid, "question", "x", shown_fingerprint=fp).status == "applied"
        again = _answer(kroot, qid, "question", "y", shown_fingerprint=fp)
        assert again.error_code == "already_answered"


# ---------------------------------------------------------------------------
# AC3: no fingerprint -> unverified on competence, held on authority (D2)
# ---------------------------------------------------------------------------


class TestNoFingerprint:
    def test_competence_item_applies_and_is_marked_unverified(self, kroot: Path) -> None:
        qid = _question(kroot)
        result = _answer(kroot, qid, "question", "x")
        assert result.status == "applied"
        assert result.outcome is not None
        assert result.outcome.evidence_strength == "unverified"
        events = read_decision_budget_events(_wiki(kroot))
        assert events[-1]["evidence"] == {"strength": "unverified"}

    def test_an_unverified_claim_without_a_fingerprint_is_read_as_unverified(
        self, kroot: Path
    ) -> None:
        # The writer refuses this; a hand-written file is read, not refused,
        # and still counts only as unverified.
        answers = kroot / "raw" / "answers"
        answers.mkdir()
        qid = _question(kroot)
        meta = {
            "source": "decision_answer",
            "decision_id": qid,
            "decision_type": "question",
            "verdict": "x",
            "evidence": {"strength": "verified", "ref": None},
        }
        (answers / "hand.md").write_text(f"---\n{yaml.safe_dump(meta)}---\n", encoding="utf-8")
        report = apply_decision_answers(_wiki(kroot), kroot / "raw")
        assert report.applied == 1
        assert report.outcomes[0].evidence_strength == "unverified"

    def test_authority_item_is_held_for_reconfirmation(self, kroot: Path) -> None:
        wiki = _wiki(kroot)
        rid = _seed_rule_proposal(wiki)
        result = _answer(kroot, rid, "proposed-rule", "approve")
        assert result.status == "refused"
        assert result.error_code == "held_unconfirmed"
        assert _item(kroot, rid) is not None  # still pending, still offered

        # Re-confirming with what was shown applies it (the newest answer
        # for the id wins on the tick, too).
        again = _answer(kroot, rid, "proposed-rule", "approve", shown_fingerprint=_fp(kroot, rid))
        assert again.status == "applied"
        assert _item(kroot, rid) is None

    def test_held_on_the_deferred_tick_as_well(self, kroot: Path) -> None:
        rid = _seed_rule_proposal(_wiki(kroot))
        write_decision_answer(
            kroot / "raw", decision_id=rid, decision_type="proposed-rule", verdict="approve"
        )
        report = apply_decision_answers(_wiki(kroot), kroot / "raw")
        assert [o.error_code for o in report.outcomes] == ["held_unconfirmed"]
        assert _item(kroot, rid) is not None

    def test_a_confirmation_is_authority_even_through_the_question_applier(
        self, kroot: Path
    ) -> None:
        # Routing comes from the pending item's own type, not the applier's.
        cid = _confirmation(kroot)
        result = _answer(kroot, cid, "question", "Confirmed.")
        assert result.error_code == "held_unconfirmed"
        ok = _answer(kroot, cid, "question", "Confirmed.", shown_fingerprint=_fp(kroot, cid))
        assert ok.status == "applied"


# ---------------------------------------------------------------------------
# AC4: an agent may not answer an authority-routed item
# ---------------------------------------------------------------------------


class TestAgentOnAuthority:
    def test_refused_even_with_a_current_fingerprint(self, kroot: Path) -> None:
        rid = _seed_rule_proposal(_wiki(kroot))
        result = _answer(
            kroot,
            rid,
            "proposed-rule",
            "approve",
            shown_fingerprint=_fp(kroot, rid),
            answerer={"id": "some-agent", "kind": "agent"},
        )
        assert result.status == "refused"
        assert result.error_code == "agent_on_authority"
        assert _item(kroot, rid) is not None

    def test_refused_through_the_cli(self, kroot: Path) -> None:
        rid = _seed_rule_proposal(_wiki(kroot))
        rc, out, _ = _cli_answer(
            kroot, rid, "proposed-rule", "approve", "--shown-fingerprint", _fp(kroot, rid),
            "--answerer-id", "lane-7", "--answerer-kind", "agent",
        )  # fmt: skip
        payload = json.loads(out)
        assert payload["ok"] is False and payload["error_code"] == "agent_on_authority"

    def test_an_agent_may_answer_a_competence_item(self, kroot: Path) -> None:
        qid = _question(kroot)
        result = _answer(
            kroot,
            qid,
            "question",
            "x",
            shown_fingerprint=_fp(kroot, qid),
            answerer={"id": "some-agent", "kind": "agent"},
        )
        assert result.status == "applied"

    def test_a_human_answerer_is_not_refused(self, kroot: Path) -> None:
        rid = _seed_rule_proposal(_wiki(kroot))
        result = _answer(
            kroot,
            rid,
            "proposed-rule",
            "approve",
            shown_fingerprint=_fp(kroot, rid),
            answerer={"id": "ana", "kind": "human"},
        )
        assert result.status == "applied"

    def test_decided_by_names_the_recorded_answerer(self, tmp_path: Path) -> None:
        def answer(**kw: Any) -> da.DecisionAnswer:
            return da.DecisionAnswer(
                decision_id="m",
                decision_type="merge",
                verdict="approve",
                note="",
                resolved_at="",
                path=tmp_path,
                **kw,
            )

        fallback = "pipeline:merge-approve"
        assert da._decided_by(answer(), fallback) == fallback
        assert da._decided_by(answer(actor="unattributed"), fallback) == "human:unattributed"
        assert da._decided_by(answer(actor="ana"), fallback) == "human:ana"
        assert (
            da._decided_by(answer(answerer={"id": "bot", "kind": "agent"}), fallback)
            == "agent:bot"
        )


# ---------------------------------------------------------------------------
# AC5: the budget-ledger answer event carries channel.host and evidence
# ---------------------------------------------------------------------------


class TestLedgerFields:
    def test_event_carries_the_channel_host_and_evidence_strength(self, kroot: Path) -> None:
        qid = _question(kroot)
        result = _answer(
            kroot,
            qid,
            "question",
            "x",
            shown_fingerprint=_fp(kroot, qid),
            channel={"host": "claude-code", "surface": "question-ui"},
            evidence={"strength": "asserted", "ref": None},
        )
        assert result.status == "applied"
        event = read_decision_budget_events(_wiki(kroot))[-1]
        assert event["v"] == DECISION_BUDGET_EVENTS_VERSION == 2
        assert event["decision_id"] == qid
        assert event["channel"] == {"host": "claude-code"}
        assert event["evidence"] == {"strength": "asserted"}

    def test_cli_event_records_host_cli(self, kroot: Path) -> None:
        rc, _out, _ = _cli_answer(kroot, _question(kroot), "question", "x")
        assert rc == 0
        event = read_decision_budget_events(_wiki(kroot))[-1]
        assert event["channel"] == {"host": "cli"}
        assert event["evidence"] == {"strength": "unverified"}

    def test_a_refused_answer_writes_no_event(self, kroot: Path) -> None:
        rid = _seed_rule_proposal(_wiki(kroot))
        _answer(kroot, rid, "proposed-rule", "approve")
        assert read_decision_budget_events(_wiki(kroot)) == []

    def test_a_version_1_event_still_reads(self, kroot: Path) -> None:
        path = _wiki(kroot) / "_decision_budget_events.jsonl"
        path.write_text(
            json.dumps(
                {
                    "v": 1,
                    "decision_id": "old",
                    "decision_type": "question",
                    "raised_at": "",
                    "answered_at": "2026-11-01T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        events = read_decision_budget_events(_wiki(kroot))
        assert [e["decision_id"] for e in events] == ["old"]
