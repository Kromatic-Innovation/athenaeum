# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum.decision_answers`` (issue athenaeum#908).

Unified decision resolution as intake: an answer is a conformant raw-intake
record carrying the decision id it resolves, applied deterministically at
tier 0 (no LLM call) on the next ``athenaeum ingest-answers`` tick. The
three per-type mutator MCP tools (``resolve_question`` / ``resolve_merge`` /
``review_audit_item``) become thin conveniences that write the same answer
files instead of mutating state directly.

Acceptance criteria under test (class -> AC):

- ``TestRenderParseRoundTrip`` / ``TestBackCompatLegacyRecords`` -> AC1, AC3
  (the format carries the decision id + covers question/merge/audit/
  proposed-rule; a legacy no-``decision_id`` record still parses as before).
- ``TestApplyQuestion`` / ``TestApplyMerge`` / ``TestApplyAudit`` -> AC2, AC4
  (deterministic tier-0 apply per type; each round-trips end to end).
- ``TestApplyProposedRule`` -> AC3, AC6 (registered + schema-round-trips;
  fails closed with zero mutation — the store itself is athenaeum#905's scope,
  explicitly NOT built here).
- ``TestBatch`` -> AC5 (several answer files, mixed types, one tick).
- ``TestFailSoft`` -> AC6 (unknown id / already-resolved id / malformed file
  — logged, uncorrupted, batch continues).
- ``TestNoLLMCall`` -> AC2's "no language-model call" guarantee.
- ``TestMutatorsDefer`` -> AC4 (the three MCP tools write answer files and
  defer; pre-flight ``error_code`` contract is unchanged).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from athenaeum.decision_answers import (
    VALID_DECISION_TYPES,
    MalformedDecisionAnswer,
    _load_decision_answer,
    apply_decision_answers,
    preflight_audit,
    preflight_merge,
    preflight_question,
    render_decision_answer,
    write_decision_answer,
)
from tests.conftest import init_git_repo

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw


def _write_question_block(
    pending_path: Path,
    *,
    date: str = "2026-04-20",
    entity: str = "Acme Corp",
    source: str = "sessions/20240406T120000Z-aabb0011.md",
    question: str = "Is Acme still Series A?",
    conflict_type: str = "principled",
    description: str = "Wiki says Series A; new raw implies Series B.",
) -> str:
    """Write a single unanswered question block; return its id."""
    block = (
        f'## [{date}] Entity: "{entity}" (from {source})\n'
        f"- [ ] {question}\n"
        f"**Conflict type**: {conflict_type}\n"
        f"**Description**: {description}\n"
    )
    pending_path.write_text("# Pending Questions\n\n" + block, encoding="utf-8")
    from athenaeum.answers import parse_pending_questions

    return parse_pending_questions(pending_path)[0].id


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\n{body}", encoding="utf-8")


def _write_merge(
    merges_path: Path,
    *,
    target: str,
    src_a: Path,
    src_b: Path,
) -> str:
    """Seed one pending-merge block via the real writer; return its id."""
    from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

    _write_source(src_a, name="alpha")
    _write_source(src_b, name="beta")
    write_pending_merge(
        merges_path,
        merge_target_name=target,
        sources=[str(src_a), str(src_b)],
        rationale="similar topic",
        draft_merged_body="merged body\n",
        confidence=0.9,
    )
    return parse_pending_merges(merges_path)[0].id


def _seed_audit_item(wiki_root: Path, *, pid: str) -> str:
    """Sample one T2-approve audit item deterministically; return its id."""
    from athenaeum.calibration import sample_tier_decision

    rec = sample_tier_decision(
        wiki_root,
        tier="T2",
        verdict="approve",
        proposal_id=pid,
        reason="calibration check",
        config={
            "librarian": {
                "audit_sample_rate_t1_rejects": 1.0,
                "audit_sample_rate_t2_approvals": 1.0,
            }
        },
    )
    assert rec is not None
    return rec["id"]


# ---------------------------------------------------------------------------
# TestRenderParseRoundTrip — AC1, AC3
# ---------------------------------------------------------------------------


class TestRenderParseRoundTrip:
    @pytest.mark.parametrize("decision_type", sorted(VALID_DECISION_TYPES))
    def test_round_trip_per_type(self, tmp_path: Path, decision_type: str) -> None:
        path = tmp_path / "answer.md"
        text = render_decision_answer(
            decision_id="abc123def456",
            decision_type=decision_type,
            verdict="approve" if decision_type == "merge" else "some verdict text",
            note="an operator note",
        )
        path.write_text(text, encoding="utf-8")

        parsed = _load_decision_answer(path)
        assert parsed is not None
        assert parsed.decision_id == "abc123def456"
        assert parsed.decision_type == decision_type
        assert parsed.note == "an operator note"
        assert parsed.resolved_at

    def test_multiline_verdict_round_trips(self, tmp_path: Path) -> None:
        """A free-text question answer body can be multi-line markdown —
        must not corrupt the YAML frontmatter block."""
        verdict = "keep_a: this is the ratified answer.\n\nSecond paragraph.\n- bullet"
        path = tmp_path / "answer.md"
        path.write_text(
            render_decision_answer(
                decision_id="q1", decision_type="question", verdict=verdict
            ),
            encoding="utf-8",
        )
        parsed = _load_decision_answer(path)
        assert parsed is not None
        assert parsed.verdict == verdict

    def test_render_rejects_invalid_decision_type(self) -> None:
        with pytest.raises(ValueError):
            render_decision_answer(
                decision_id="x", decision_type="not-a-real-type", verdict="v"
            )

    def test_render_rejects_empty_decision_id(self) -> None:
        with pytest.raises(ValueError):
            render_decision_answer(decision_id="", decision_type="question", verdict="v")

    def test_write_decision_answer_creates_file_under_answers_dir(
        self, raw_root: Path
    ) -> None:
        path = write_decision_answer(
            raw_root, decision_id="m1", decision_type="merge", verdict="approve"
        )
        assert path.parent == raw_root / "answers"
        assert path.exists()
        parsed = _load_decision_answer(path)
        assert parsed is not None
        assert parsed.decision_id == "m1"

    def test_write_decision_answer_collision_gets_suffix(self, raw_root: Path) -> None:
        p1 = write_decision_answer(
            raw_root, decision_id="dup", decision_type="merge", verdict="approve"
        )
        p2 = write_decision_answer(
            raw_root, decision_id="dup", decision_type="merge", verdict="reject"
        )
        assert p1 != p2
        assert p1.exists() and p2.exists()


class TestBackCompatLegacyRecords:
    """A legacy ``pending_question_answer`` record (no ``decision_id``) must
    parse exactly as it does today — i.e. be silently ignored by the
    decision-answer applier, never treated as a decision answer."""

    def test_legacy_record_has_no_decision_id(self, tmp_path: Path) -> None:
        from athenaeum.answers import PendingQuestion, _render_answer_raw_file

        pq = PendingQuestion(
            id="legacy1",
            entity="Acme Corp",
            source="sessions/x.md",
            question="Q?",
            conflict_type="principled",
            description="d",
            created_at="2026-01-01",
            answered=True,
            answer_lines=["Series B."],
            raw_block="## ...",
        )
        text = _render_answer_raw_file(pq, "2026-01-01T00:00:00Z")
        path = tmp_path / "legacy.md"
        path.write_text(text, encoding="utf-8")

        assert _load_decision_answer(path) is None

    def test_apply_skips_legacy_records_untouched(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.answers import PendingQuestion, _render_answer_raw_file

        answers_dir = raw_root / "answers"
        answers_dir.mkdir(parents=True)
        pq = PendingQuestion(
            id="legacy2",
            entity="Acme Corp",
            source="sessions/x.md",
            question="Q?",
            conflict_type="principled",
            description="d",
            created_at="2026-01-01",
            answered=True,
            answer_lines=["Series B."],
            raw_block="## ...",
        )
        legacy_path = answers_dir / "legacy.md"
        legacy_path.write_text(
            _render_answer_raw_file(pq, "2026-01-01T00:00:00Z"), encoding="utf-8"
        )
        before = legacy_path.read_text(encoding="utf-8")

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.skipped == 0
        assert legacy_path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# TestApplyQuestion — AC2, AC4
# ---------------------------------------------------------------------------


class TestApplyQuestion:
    def test_apply_marks_block_answered(self, wiki_root: Path, raw_root: Path) -> None:
        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        write_decision_answer(
            raw_root, decision_id=qid, decision_type="question", verdict="Series B."
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 0
        text = pending_path.read_text(encoding="utf-8")
        assert "- [x]" in text
        assert "Series B." in text

    def test_full_tick_completes_writeback_and_archive(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """Applying the decision answer, then running the existing
        ``ingest_answers`` pass right after (mirrors ``cmd_ingest_answers``'s
        wiring) completes the write-back + archival — AC2's "next tick"."""
        from athenaeum.answers import ingest_answers

        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        write_decision_answer(
            raw_root, decision_id=qid, decision_type="question", verdict="Series B."
        )

        decision_report = apply_decision_answers(wiki_root, raw_root)
        assert decision_report.applied == 1
        count = ingest_answers(pending_path, raw_root, client=None, config=None)

        assert count == 1
        assert (wiki_root / "_pending_questions_archive.md").exists()
        # The primary file no longer carries the resolved block.
        assert "Is Acme still Series A?" not in pending_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestApplyMerge — AC2, AC4
# ---------------------------------------------------------------------------


class TestApplyMerge:
    def test_apply_reject_flips_checkbox(self, wiki_root: Path, raw_root: Path) -> None:
        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="merged-topic",
            src_a=wiki_root / "feedback_alpha.md",
            src_b=wiki_root / "feedback_beta.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="reject"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        text = merges_path.read_text(encoding="utf-8")
        assert "- [x]" in text
        assert "**Decision**: reject" in text

    def test_apply_approve_writes_merged_target_page(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="approved-topic",
            src_a=wiki_root / "feedback_gamma.md",
            src_b=wiki_root / "feedback_delta.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        target = wiki_root / "approved-topic.md"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "merged body"

    def test_apply_uppercase_verdict_normalized(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """``verdict`` for a merge is normalized (case/whitespace) before
        dispatch, so an answer file hand-authored with 'Approve' still
        applies instead of failing invalid_decision."""
        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="norm-topic",
            src_a=wiki_root / "feedback_e.md",
            src_b=wiki_root / "feedback_f.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="  Reject  "
        )

        report = apply_decision_answers(wiki_root, raw_root)
        assert report.applied == 1


# ---------------------------------------------------------------------------
# TestVerdictLedgerWiring — issue athenaeum#712 Wiring AC.
#
# The verdict ledger is "consumed within this same issue by writing verdicts
# for the decisions the current pipeline already makes": a merge
# approve/reject applied here (the SAME apply_decision_answers path
# `athenaeum ingest-answers` drives) records a verdict when
# `librarian.verdict_ledger_enabled` is on AND the caller passes its
# already-acquired lock — and does neither when the flag is off (byte-
# identical to before athenaeum#712) or no lock is supplied (every
# pre-athenaeum#712 caller).
# ---------------------------------------------------------------------------


class TestVerdictLedgerWiring:
    def test_flag_off_writes_no_verdict(self, wiki_root: Path, raw_root: Path) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.verdicts import ledger_exists

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="flag-off-topic",
            src_a=wiki_root / "feedback_off_a.md",
            src_b=wiki_root / "feedback_off_b.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )

        lock = RunLock(wiki_root.parent)
        with lock:
            report = apply_decision_answers(
                wiki_root,
                raw_root,
                config={"librarian": {"verdict_ledger_enabled": False}},
                lock=lock,
            )
        assert report.applied == 1
        assert ledger_exists(wiki_root) is False

    def test_flag_on_with_lock_records_duplicate_verdict_on_approve(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.verdicts import list_by_verdict

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="flag-on-topic",
            src_a=wiki_root / "feedback_on_a.md",
            src_b=wiki_root / "feedback_on_b.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )

        lock = RunLock(wiki_root.parent)
        with lock:
            report = apply_decision_answers(
                wiki_root,
                raw_root,
                config={"librarian": {"verdict_ledger_enabled": True}},
                lock=lock,
            )
        assert report.applied == 1
        entries = list_by_verdict(wiki_root)
        assert len(entries) == 1
        assert entries[0]["verdict"] == "duplicate"

    def test_flag_on_with_lock_records_distinct_verdict_on_reject(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.verdicts import list_by_verdict

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="flag-on-reject-topic",
            src_a=wiki_root / "feedback_rej_a.md",
            src_b=wiki_root / "feedback_rej_b.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="reject"
        )

        lock = RunLock(wiki_root.parent)
        with lock:
            report = apply_decision_answers(
                wiki_root,
                raw_root,
                config={"librarian": {"verdict_ledger_enabled": True}},
                lock=lock,
            )
        assert report.applied == 1
        entries = list_by_verdict(wiki_root)
        assert len(entries) == 1
        assert entries[0]["verdict"] == "distinct"

    def test_flag_on_without_lock_writes_no_verdict(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """No lock supplied (every pre-athenaeum#712 caller) -> no ledger write,
        even with the flag on — see apply_decision_answers's `lock` docstring."""
        from athenaeum.verdicts import ledger_exists

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="no-lock-topic",
            src_a=wiki_root / "feedback_nl_a.md",
            src_b=wiki_root / "feedback_nl_b.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )

        report = apply_decision_answers(
            wiki_root,
            raw_root,
            config={"librarian": {"verdict_ledger_enabled": True}},
        )
        assert report.applied == 1
        assert ledger_exists(wiki_root) is False


# ---------------------------------------------------------------------------
# TestFoldRecoverability — issue athenaeum#947 AC4.
#
# Drives the REAL deferred (MCP-shaped) path end to end: write a
# decision-answer file approving a fold-into-existing merge (the exact
# shape mcp_server.resolve_merge produces via write_decision_answer), apply
# it through apply_decision_answers (the same function
# _cmd_pending.cmd_ingest_answers calls after acquiring the run lock), and
# assert the folded-away page is recoverable from git history afterward.
# ---------------------------------------------------------------------------


class TestFoldRecoverability:
    def test_folded_source_recoverable_from_git_history(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        target_path = wiki_root / "canonical-topic.md"
        target_path.write_text(
            "---\nname: Canonical Topic\ntype: concept\n---\n"
            "ORIGINAL CANONICAL PROSE\n",
            encoding="utf-8",
        )
        src_path = wiki_root / "old-source.md"
        src_path.write_text(
            "---\nname: Old Source\ntype: feedback\n---\n"
            "THE ORIGINAL SOURCE CONTENT\n",
            encoding="utf-8",
        )
        init_git_repo(wiki_root)

        from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

        merges_path = wiki_root / "_pending_merges.md"
        # write_kind is left to derive (target already exists at this slug,
        # so classify_write_kind derives fold-into-existing — mirrors the
        # real proposal-time path, not a hand-set override).
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical Topic",
            sources=[str(src_path)],
            rationale="consolidate duplicate",
            draft_merged_body="MERGED PROSE\n",
            confidence=0.9,
        )
        mid = parse_pending_merges(merges_path)[0].id
        assert parse_pending_merges(merges_path)[0].write_kind == "fold-into-existing"

        # Same shape mcp_server.resolve_merge produces (issue athenaeum#908):
        # a decision-answer file under raw/answers/ — NOT a synchronous
        # apply. Nothing in the wiki has moved yet at this point.
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )
        assert src_path.exists()

        # Apply through the same deferred path _cmd_pending.cmd_ingest_answers
        # drives (after acquiring the CLI run lock — see
        # pending_merges._apply_fold_into_existing's docstring, AC3).
        report = apply_decision_answers(wiki_root, raw_root)
        assert report.applied == 1
        assert report.skipped == 0

        # Working tree: the folded-away source is gone; the target survived
        # with the merged content.
        assert not src_path.exists()
        assert target_path.exists()
        assert "MERGED PROSE" in target_path.read_text(encoding="utf-8")

        # Git history: exactly one commit deleted old-source.md, and the
        # ORIGINAL content (from before that commit) is still recoverable —
        # the README.md "a bad merge is a `git revert` away" guarantee.
        rel_src = "old-source.md"
        log = subprocess.run(
            ["git", "log", "--diff-filter=D", "--format=%H", "--", rel_src],
            cwd=str(wiki_root),
            capture_output=True,
            text=True,
            check=True,
        )
        deleting_commits = [c for c in log.stdout.splitlines() if c]
        assert len(deleting_commits) == 1
        deleting_commit = deleting_commits[0]

        recovered = subprocess.run(
            ["git", "show", f"{deleting_commit}^:{rel_src}"],
            cwd=str(wiki_root),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "THE ORIGINAL SOURCE CONTENT" in recovered.stdout


# ---------------------------------------------------------------------------
# TestApplyAudit — AC2, AC4
# ---------------------------------------------------------------------------


class TestApplyAudit:
    def test_apply_records_review(self, wiki_root: Path, raw_root: Path) -> None:
        from athenaeum.calibration import read_calibration_ledger

        audit_id = _seed_audit_item(wiki_root, pid="proposal-1")
        write_decision_answer(
            raw_root, decision_id=audit_id, decision_type="audit", verdict="approve"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        records = read_calibration_ledger(wiki_root)
        reviews = [r for r in records if r.get("kind") == "review"]
        assert len(reviews) == 1
        assert reviews[0]["id"] == audit_id
        assert reviews[0]["overturned"] is False  # verdict matches sampled verdict


# ---------------------------------------------------------------------------
# TestApplyProposedRule — AC3, AC6
# ---------------------------------------------------------------------------


class TestApplyProposedRule:
    def test_schema_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "rule.md"
        path.write_text(
            render_decision_answer(
                decision_id="rule-1",
                decision_type="proposed-rule",
                verdict="accept",
            ),
            encoding="utf-8",
        )
        parsed = _load_decision_answer(path)
        assert parsed is not None
        assert parsed.decision_type == "proposed-rule"

    def test_apply_fails_closed_with_zero_mutation(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        write_decision_answer(
            raw_root,
            decision_id="rule-1",
            decision_type="proposed-rule",
            verdict="accept",
        )

        # Snapshot every file under wiki_root/raw_root before applying —
        # the fail-closed branch must not create/modify/delete anything
        # beyond the answer file itself.
        def _snapshot() -> set[tuple[str, str]]:
            out = set()
            for root in (wiki_root, raw_root):
                for p in root.rglob("*"):
                    if p.is_file():
                        out.add((str(p), p.read_text(encoding="utf-8", errors="replace")))
            return out

        before = _snapshot()
        report = apply_decision_answers(wiki_root, raw_root)
        after = _snapshot()

        assert report.applied == 0
        assert report.skipped == 1
        outcome = report.outcomes[0]
        assert outcome.error_code == "decision_type_unavailable"
        assert "905" in outcome.message
        assert before == after


# ---------------------------------------------------------------------------
# TestBatch — AC5
# ---------------------------------------------------------------------------


class TestBatch:
    def test_no_answers_dir_is_a_clean_noop(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """``raw_root/answers`` not existing at all (nothing has ever
        written an answer file) must not error — it's an empty batch."""
        report = apply_decision_answers(wiki_root, raw_root)
        assert report.applied == 0
        assert report.skipped == 0
        assert report.outcomes == []

    def test_mixed_batch_applies_in_one_tick(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="batch-topic",
            src_a=wiki_root / "feedback_batch_a.md",
            src_b=wiki_root / "feedback_batch_b.md",
        )

        audit_id = _seed_audit_item(wiki_root, pid="batch-proposal")

        write_decision_answer(
            raw_root, decision_id=qid, decision_type="question", verdict="Series B."
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )
        write_decision_answer(
            raw_root, decision_id=audit_id, decision_type="audit", verdict="approve"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 3
        assert report.skipped == 0
        assert "- [x]" in pending_path.read_text(encoding="utf-8")
        assert "- [x]" in merges_path.read_text(encoding="utf-8")
        assert (wiki_root / "batch-topic.md").exists()

    def test_batch_via_cmd_ingest_answers_end_to_end(self, tmp_path: Path) -> None:
        """The real CLI entry point applies decision answers AND completes
        the legacy question write-back/archive in one locked invocation."""
        import argparse

        from athenaeum._cmd_pending import cmd_ingest_answers

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        pending_path = wiki / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        write_decision_answer(
            raw, decision_id=qid, decision_type="question", verdict="Series B."
        )

        rc = cmd_ingest_answers(argparse.Namespace(path=tmp_path))

        assert rc == 0
        assert (wiki / "_pending_questions_archive.md").exists()


# ---------------------------------------------------------------------------
# TestFailSoft — AC6
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_unknown_question_id_logged_and_skipped(
        self, wiki_root: Path, raw_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # File exists but carries no matching block, so the outcome is a
        # genuine "id not found" rather than "file missing".
        (wiki_root / "_pending_questions.md").write_text(
            "# Pending Questions\n\n", encoding="utf-8"
        )
        write_decision_answer(
            raw_root, decision_id="no-such-id", decision_type="question", verdict="x"
        )
        with caplog.at_level(logging.INFO, logger="athenaeum.decision_answers"):
            report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.skipped == 1
        assert report.outcomes[0].error_code == "id_not_found"
        assert any("no-such-id" in rec.message for rec in caplog.records)

    def test_already_answered_question_logged_and_skipped_without_reapplying(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        # Answer it once, directly (bypassing the decision-answer path) so
        # it is already answered by the time the decision-answer file lands.
        from athenaeum.answers import resolve_by_id

        first = resolve_by_id(pending_path, qid, "already answered directly")
        assert first["ok"] is True

        write_decision_answer(
            raw_root,
            decision_id=qid,
            decision_type="question",
            verdict="a second, different answer",
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "already_answered"
        # The original answer must survive untouched — no corruption.
        text = pending_path.read_text(encoding="utf-8")
        assert "already answered directly" in text
        assert "a second, different answer" not in text

    def test_already_resolved_merge_logged_and_skipped(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.pending_merges import resolve_merge

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="already-topic",
            src_a=wiki_root / "feedback_g.md",
            src_b=wiki_root / "feedback_h.md",
        )
        first = resolve_merge(merges_path, mid, "reject")
        assert first["ok"] is True

        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "already_resolved"
        # Original reject decision untouched; no approve target written.
        assert not (wiki_root / "already-topic.md").exists()

    def test_already_reviewed_audit_logged_and_skipped(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.calibration import record_audit_review

        audit_id = _seed_audit_item(wiki_root, pid="dup-proposal")
        record_audit_review(wiki_root, audit_id=audit_id, human_verdict="approve")

        write_decision_answer(
            raw_root, decision_id=audit_id, decision_type="audit", verdict="reject"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "already_resolved"

    def test_malformed_answer_file_skipped_batch_continues(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        answers_dir = raw_root / "answers"
        answers_dir.mkdir(parents=True)
        # Malformed: decision_id present but decision_type missing.
        (answers_dir / "20260101T000000Z-bad.md").write_text(
            "---\nsource: decision_answer\ndecision_id: bad1\nverdict: x\n"
            "resolved_at: 2026-01-01T00:00:00Z\n---\n\nbody\n",
            encoding="utf-8",
        )

        # A good file in the same batch must still apply.
        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        write_decision_answer(
            raw_root, decision_id=qid, decision_type="question", verdict="Series B."
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 1
        malformed_outcomes = [o for o in report.outcomes if o.error_code == "malformed"]
        assert len(malformed_outcomes) == 1
        assert malformed_outcomes[0].decision_id is None
        # The malformed file is left in place, not deleted (audit trail).
        assert (answers_dir / "20260101T000000Z-bad.md").exists()

    def test_invalid_merge_verdict_rejected_without_mutation(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="invalid-verdict-topic",
            src_a=wiki_root / "feedback_i.md",
            src_b=wiki_root / "feedback_j.md",
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="maybe"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "invalid_decision"
        assert "- [ ]" in merges_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestNoLLMCall — AC2 ("no language-model call")
# ---------------------------------------------------------------------------


class TestNoLLMCall:
    def test_apply_never_calls_the_llm_backend(
        self, wiki_root: Path, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import provider

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                "apply_decision_answers must never touch the LLM backend"
            )

        monkeypatch.setattr(provider, "build_llm_client", _boom)

        pending_path = wiki_root / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="no-llm-topic",
            src_a=wiki_root / "feedback_k.md",
            src_b=wiki_root / "feedback_l.md",
        )
        audit_id = _seed_audit_item(wiki_root, pid="no-llm-proposal")

        write_decision_answer(
            raw_root, decision_id=qid, decision_type="question", verdict="Series B."
        )
        write_decision_answer(
            raw_root, decision_id=mid, decision_type="merge", verdict="approve"
        )
        write_decision_answer(
            raw_root, decision_id=audit_id, decision_type="audit", verdict="approve"
        )
        write_decision_answer(
            raw_root, decision_id="rule-1", decision_type="proposed-rule", verdict="accept"
        )

        # Must not raise -- proves the LLM entry point was never invoked.
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 3  # question + merge + audit
        assert report.skipped == 1  # proposed-rule fails closed


# ---------------------------------------------------------------------------
# TestMutatorsDefer — AC4
# ---------------------------------------------------------------------------


class TestMutatorsDefer:
    def _server(self, tmp_path: Path, *, wiki: Path, raw: Path):
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        return create_server(raw_root=raw, wiki_root=wiki)

    def _call(self, server, name: str, caller):
        import asyncio

        async def _run():
            tool = await server.get_tool(name)
            return caller(tool.fn)

        return asyncio.run(_run())

    def test_resolve_question_defers_and_writes_answer_file(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        pending_path = wiki / "_pending_questions.md"
        qid = _write_question_block(pending_path)

        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(
            server, "resolve_question", lambda fn: fn(qid, "Series B.")
        )

        assert result["ok"] is True
        assert result["deferred"] is True
        assert result["decision_id"] == qid
        answer_file = Path(result["answer_file"])
        assert answer_file.exists()
        # Not yet applied -- checkbox still unchecked.
        assert "- [x]" not in pending_path.read_text(encoding="utf-8")

    def test_resolve_question_preflight_still_reports_already_answered(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        pending_path = wiki / "_pending_questions.md"
        qid = _write_question_block(pending_path)
        from athenaeum.answers import resolve_by_id

        resolve_by_id(pending_path, qid, "already answered")

        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(server, "resolve_question", lambda fn: fn(qid, "second"))

        assert result["ok"] is False
        assert result["error_code"] == "already_answered"
        # Nothing written on the error path.
        assert not (raw / "answers").exists()

    def test_resolve_merge_defers_and_writes_answer_file(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        merges_path = wiki / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="mcp-topic",
            src_a=wiki / "feedback_mcp_a.md",
            src_b=wiki / "feedback_mcp_b.md",
        )

        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(
            server, "resolve_merge", lambda fn: fn(mid, "approve", "note text")
        )

        assert result["ok"] is True
        assert result["deferred"] is True
        answer_file = Path(result["answer_file"])
        assert answer_file.exists()
        # Not yet applied -- no target page written, checkbox unflipped.
        assert not (wiki / "mcp-topic.md").exists()
        assert "- [x]" not in merges_path.read_text(encoding="utf-8")

    def test_resolve_merge_preflight_still_reports_invalid_decision(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(
            server, "resolve_merge", lambda fn: fn("whatever", "bogus-decision")
        )
        assert result["ok"] is False
        assert result["error_code"] == "invalid_decision"
        assert not (raw / "answers").exists()

    def test_review_audit_item_defers_and_writes_answer_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_TIER_AUDITING_ENABLED", "1")
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        audit_id = _seed_audit_item(wiki, pid="mcp-proposal")

        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(
            server, "review_audit_item", lambda fn: fn(audit_id, "approve")
        )

        assert result["ok"] is True
        assert result["deferred"] is True
        answer_file = Path(result["answer_file"])
        assert answer_file.exists()
        # Not yet applied -- no review record in the ledger.
        from athenaeum.calibration import read_calibration_ledger

        records = read_calibration_ledger(wiki)
        assert not [r for r in records if r.get("kind") == "review"]

    def test_review_audit_item_preflight_still_reports_unknown_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_REASONING_TIER_AUDITING_ENABLED", "1")
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        raw.mkdir()
        server = self._server(tmp_path, wiki=wiki, raw=raw)
        result = self._call(
            server, "review_audit_item", lambda fn: fn("no-such-id", "approve")
        )
        assert result["ok"] is False
        assert result["error_code"] == "id_not_found"


# ---------------------------------------------------------------------------
# Preflight helpers (used directly by the mutators; light unit coverage)
# ---------------------------------------------------------------------------


class TestPreflightHelpers:
    def test_preflight_question_file_missing(self, tmp_path: Path) -> None:
        ok, code, _ = preflight_question(tmp_path / "nope.md", "any-id")
        assert ok is False
        assert code == "file_missing"

    def test_preflight_merge_invalid_decision_short_circuits_before_file_check(
        self, tmp_path: Path
    ) -> None:
        ok, code, _ = preflight_merge(tmp_path / "nope.md", "any-id", "bogus")
        assert ok is False
        assert code == "invalid_decision"

    def test_preflight_audit_unknown_id(self, wiki_root: Path) -> None:
        ok, code, _ = preflight_audit(wiki_root, "no-such-id")
        assert ok is False
        assert code == "id_not_found"

    def test_preflight_question_id_not_found_with_file_present(
        self, wiki_root: Path
    ) -> None:
        pending_path = wiki_root / "_pending_questions.md"
        _write_question_block(pending_path)
        ok, code, _ = preflight_question(pending_path, "no-such-id")
        assert ok is False
        assert code == "id_not_found"

    def test_preflight_merge_file_missing(self, wiki_root: Path) -> None:
        ok, code, _ = preflight_merge(
            wiki_root / "_pending_merges.md", "any-id", "approve"
        )
        assert ok is False
        assert code == "file_missing"

    def test_preflight_merge_id_not_found_with_file_present(
        self, wiki_root: Path
    ) -> None:
        merges_path = wiki_root / "_pending_merges.md"
        _write_merge(
            merges_path,
            target="preflight-topic",
            src_a=wiki_root / "feedback_pf_a.md",
            src_b=wiki_root / "feedback_pf_b.md",
        )
        ok, code, _ = preflight_merge(merges_path, "no-such-id", "approve")
        assert ok is False
        assert code == "id_not_found"

    def test_preflight_merge_already_resolved(self, wiki_root: Path) -> None:
        from athenaeum.pending_merges import resolve_merge

        merges_path = wiki_root / "_pending_merges.md"
        mid = _write_merge(
            merges_path,
            target="preflight-resolved-topic",
            src_a=wiki_root / "feedback_pf_c.md",
            src_b=wiki_root / "feedback_pf_d.md",
        )
        resolve_merge(merges_path, mid, "reject")
        ok, code, _ = preflight_merge(merges_path, mid, "approve")
        assert ok is False
        assert code == "already_resolved"

    def test_preflight_audit_already_resolved(self, wiki_root: Path) -> None:
        from athenaeum.calibration import record_audit_review

        audit_id = _seed_audit_item(wiki_root, pid="preflight-proposal")
        record_audit_review(wiki_root, audit_id=audit_id, human_verdict="approve")
        ok, code, _ = preflight_audit(wiki_root, audit_id)
        assert ok is False
        assert code == "already_resolved"


class TestLoadDecisionAnswerEdgeCases:
    """Direct unit coverage of ``_load_decision_answer``'s schema-validation
    branches (each corresponds to one flavor of "malformed" AC6 covers via
    ``apply_decision_answers``'s batch loop)."""

    def test_unreadable_file_raises_malformed(self, tmp_path: Path) -> None:
        # A directory is not readable as text -- read_text() raises OSError.
        a_directory = tmp_path / "not-a-file.md"
        a_directory.mkdir()
        with pytest.raises(MalformedDecisionAnswer):
            _load_decision_answer(a_directory)

    def test_empty_decision_id_raises_malformed(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text(
            "---\nsource: decision_answer\ndecision_id: \"\"\n"
            "decision_type: question\nverdict: x\n---\n\nbody\n",
            encoding="utf-8",
        )
        with pytest.raises(MalformedDecisionAnswer):
            _load_decision_answer(path)

    def test_missing_verdict_raises_malformed(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text(
            "---\nsource: decision_answer\ndecision_id: q1\n"
            "decision_type: question\n---\n\nbody\n",
            encoding="utf-8",
        )
        with pytest.raises(MalformedDecisionAnswer):
            _load_decision_answer(path)

    def test_render_rejects_empty_verdict(self) -> None:
        with pytest.raises(ValueError):
            render_decision_answer(decision_id="x", decision_type="question", verdict="")
