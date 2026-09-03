# SPDX-License-Identifier: Apache-2.0
"""T2 wired into the merge path, auto-finalizing safe-class approvals (athenaeum#602).

Mirrors ``test_merge_reasoning_wiring.py`` (T1's own wiring test), but for
``reasoning_screens.t2_screen_merge_proposal``: a T1 pass-up is consulted by T2, and a
safe-class ``approve`` auto-applies the merge — bypassing the human
``_pending_merges.md`` queue — via the EXACT SAME
``pending_merges.resolve_merge`` approve-time fold every human approval uses,
marked ``auto_applied`` in provenance.

FAIL-SAFE DIRECTION IS ABSOLUTE (the issue's own words): every failure mode
below is asserted to reach the human queue (`_pending_merges.md` gets an
UNRESOLVED block) and NOT the wiki (no `wiki/<slug>.md` is written, no
provenance record is written). The eight adversarial cases named in the
issue's acceptance criteria each get an explicit test in
``TestFailSafeDirection`` below.

Re-pointed by issue athenaeum#1257: the screen moved from ``athenaeum.merge``
to ``athenaeum.reasoning_screens``. T2 relocated WITH T1 but, unlike T1, is
deliberately not wired into the cluster-domain comparator lane — it keeps only
its existing C4 call site. Every assertion below is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import reasoning_screens as screens_mod
from athenaeum.calibration import calibration_summary, record_audit_review
from athenaeum.models import TokenUsage
from athenaeum.pending_merges import parse_pending_merges
from athenaeum.provenance import read_merge_provenance
from athenaeum.reasoning_screens import t2_screen_merge_proposal
from athenaeum.reasoning_tiers import (
    SAFE_CLASS_VIOLATION_AXIOM_MEMBER,
    SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS,
    SAFE_CLASS_VIOLATION_PII_FLAGGED,
    SAFE_CLASS_VIOLATION_TOO_MANY_PAGES,
)

# High sample rate so a T2 approve is always surfaced to the audit ledger.
_SAMPLE_CFG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
    }
}


def _write_source(
    path: Path,
    *,
    name: str,
    memory_class: str | None = "fact",
    pii: bool = False,
    body: str = "A short body about this entity, with a bit more detail.",
) -> str:
    lines = ["---", f"name: {name}"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    if pii:
        lines.append("pii: true")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return str(path)


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = resp
    return client


def _approve_client() -> MagicMock:
    return _mock_client(
        '{"verdict": "approve", "reason": "safe, homogeneous cluster", '
        '"amended_sources": null, "drafted_body": null}'
    )


def _wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir(exist_ok=True)
    return w


def _safe_pair(tmp_path: Path) -> list[str]:
    return [
        _write_source(tmp_path / "a.md", name="Alpha"),
        _write_source(tmp_path / "b.md", name="Beta"),
    ]


def _screen(tmp_path: Path, *, member_paths: list[str] | None = None, **overrides):
    if member_paths is None:
        member_paths = _safe_pair(tmp_path)
    kwargs = dict(
        member_paths=member_paths,
        merge_target_name="Merged Topic",
        rationale="these two sources describe the same thing",
        draft_merged_body="## Merged\n\nsynthesized body",
        confidence=0.9,
        write_kind="create-merged",
        cluster_id="c1",
        client=_approve_client(),
        usage=TokenUsage(),
        wiki_root=_wiki(tmp_path),
        config=_SAMPLE_CFG,
        provider="claude-cli",
        authority_manifest=None,
        enabled=True,
        dry_run=False,
    )
    kwargs.update(overrides)
    return t2_screen_merge_proposal(**kwargs), kwargs


def _assert_reached_human_queue_not_wiki(
    wiki_root: Path, *, target_slug: str = "merged-topic"
) -> None:
    """Shared assertion: an unresolved block exists, nothing landed on disk."""
    merges_path = wiki_root / "_pending_merges.md"
    pms = parse_pending_merges(merges_path)
    assert len(pms) == 1, "expected exactly one pending-merge block"
    assert pms[0].resolved is False, "block must be UNRESOLVED (human queue)"
    assert not (wiki_root / f"{target_slug}.md").exists(), (
        "no wiki page may exist — nothing was ever approved"
    )
    assert read_merge_provenance(wiki_root) == [], (
        "no provenance record — nothing was ever finalized"
    )


# ---------------------------------------------------------------------------
# Happy path — safe-class approve auto-applies, marked in provenance.
# ---------------------------------------------------------------------------


class TestHappyPathAutoFinalize:
    def test_safe_class_approve_auto_applies_and_bypasses_human_queue(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        handled, kwargs = _screen(tmp_path, wiki_root=wiki)
        assert handled is True

        # The wiki page IS written — auto-applied, not merely proposed.
        target = wiki / "merged-topic.md"
        assert target.exists()
        assert "synthesized body" in target.read_text(encoding="utf-8")

        # The pending-merge block exists but is RESOLVED (approve), not
        # sitting in the human queue unresolved.
        merges_path = wiki / "_pending_merges.md"
        pms = parse_pending_merges(merges_path)
        assert len(pms) == 1
        assert pms[0].resolved is True
        assert pms[0].decision == "approve"
        assert pms[0].auto_applied is True
        assert "**Auto-applied**: true" in pms[0].raw_block

    def test_provenance_marks_auto_applied_distinct_from_human_approve(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        _screen(tmp_path, wiki_root=wiki)
        records = read_merge_provenance(wiki)
        assert len(records) == 1
        assert records[0]["auto_applied"] is True

        # Contrast: a human approve via resolve_merge directly (unchanged
        # call, default auto_applied=False) never sets the marker.
        from athenaeum.pending_merges import resolve_merge, write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Human Approved Topic",
            sources=_safe_pair(tmp_path),
            rationale="human reviewed this",
            draft_merged_body="human body",
            confidence=0.8,
        )
        from athenaeum.pending_merges import _make_id

        human_id = _make_id(_safe_pair(tmp_path), "Human Approved Topic")
        result = resolve_merge(wiki / "_pending_merges.md", human_id, "approve")
        assert result["ok"] is True
        human_records = [
            r for r in read_merge_provenance(wiki) if r["canonical_slug"] != "merged-topic"
        ]
        assert len(human_records) == 1
        assert human_records[0]["auto_applied"] is False

    def test_calibration_samples_the_auto_applied_approve(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        _screen(tmp_path, wiki_root=wiki)
        summary = calibration_summary(wiki)
        assert summary["T2"]["sampled"] == 1
        assert summary["T2"]["applied"] == 1


# ---------------------------------------------------------------------------
# Overturn of an APPLIED merge — recorded and surfaced distinctly (AC5).
# ---------------------------------------------------------------------------


class TestOverturnOfAppliedMerge:
    def test_overturning_a_sampled_auto_applied_approve_is_recorded_distinctly(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        _screen(tmp_path, wiki_root=wiki)
        from athenaeum.calibration import list_pending_audit

        pending = list_pending_audit(wiki)
        assert len(pending) == 1
        audit_id = pending[0]["id"]
        assert pending[0]["applied"] is True

        review = record_audit_review(wiki, audit_id=audit_id, human_verdict="escalate")
        assert review["overturned"] is True
        assert review["applied"] is True
        assert review["overturned_applied"] is True

        summary = calibration_summary(wiki)
        assert summary["T2"]["overturned"] == 1
        assert summary["T2"]["overturned_applied"] == 1

        # Automated unwinding is explicitly OUT OF SCOPE: the wiki page
        # written by the auto-apply is untouched by the overturn review.
        assert (wiki / "merged-topic.md").exists()

    def test_confirming_an_applied_approve_does_not_count_as_overturned_applied(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        _screen(tmp_path, wiki_root=wiki)
        from athenaeum.calibration import list_pending_audit

        audit_id = list_pending_audit(wiki)[0]["id"]
        review = record_audit_review(wiki, audit_id=audit_id, human_verdict="approve")
        assert review["overturned"] is False
        assert review["overturned_applied"] is False
        summary = calibration_summary(wiki)
        assert summary["T2"]["overturned_applied"] == 0

    def test_t1_reject_audit_item_is_never_applied(self, tmp_path: Path) -> None:
        # Sanity check on the flip side: a T1 reject is never "applied" (a
        # reject writes nothing), so an overturn of one can never be
        # overturned_applied even though it IS overturned.
        from athenaeum.calibration import sample_tier_decision

        wiki = _wiki(tmp_path)
        rec = sample_tier_decision(
            wiki,
            tier="T1",
            verdict="reject",
            proposal_id="p-t1",
            reason="different entities",
            config=_SAMPLE_CFG,
        )
        assert rec is not None
        assert rec["applied"] is False
        review = record_audit_review(wiki, audit_id=rec["id"], human_verdict="pass_up")
        assert review["overturned"] is True
        assert review["overturned_applied"] is False


# ---------------------------------------------------------------------------
# FAIL-SAFE DIRECTION — the 8 adversarial cases named in the issue's ACs.
# Every one of these must reach the human queue and NOT the wiki.
# ---------------------------------------------------------------------------


class TestFailSafeDirection:
    def test_ceiling_tripped_degrades_to_human_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            screens_mod.spend, "ceiling_tripped", lambda *a, **k: "budget"
        )
        wiki = _wiki(tmp_path)
        client = _approve_client()
        handled, _ = _screen(tmp_path, wiki_root=wiki, client=client)
        assert handled is False  # caller falls through to write_pending_merge
        client.messages.create.assert_not_called()
        # Caller-side contract: t2_screen returning False means the caller
        # still writes the proposal unscreened. Simulate that write here to
        # assert the end state matches "reached human queue, not wiki".
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=_safe_pair(tmp_path),
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)

    def test_unparseable_verdict_escalates_to_human_queue(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        client = _mock_client("not json at all, sorry")
        handled, _ = _screen(tmp_path, wiki_root=wiki, client=client)
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=_safe_pair(tmp_path),
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)

    def test_unexpected_verdict_string_escalates_to_human_queue(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        client = _mock_client(
            '{"verdict": "definitely_approve_trust_me", "reason": "sneaky"}'
        )
        handled, _ = _screen(tmp_path, wiki_root=wiki, client=client)
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=_safe_pair(tmp_path),
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)

    def test_tier_disabled_never_auto_applies(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        client = _approve_client()
        handled, _ = _screen(tmp_path, wiki_root=wiki, enabled=False, client=client)
        assert handled is False
        client.messages.create.assert_not_called()
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=_safe_pair(tmp_path),
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)

    def test_oversized_cluster_safe_class_violation_escalates(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.reasoning_tiers import SAFE_CLASS_MAX_PAGES

        wiki = _wiki(tmp_path)
        sources = [
            _write_source(tmp_path / f"s{i}.md", name=f"S{i}")
            for i in range(SAFE_CLASS_MAX_PAGES + 1)
        ]
        client = _approve_client()  # model tries to approve; gate blocks it
        handled, _ = _screen(
            tmp_path, wiki_root=wiki, member_paths=sources, client=client
        )
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=sources,
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)
        # Confirm this really was the too-many-pages violation and not some
        # other escalation path.
        from athenaeum.reasoning_tiers import read_reasoning_tier_decisions

        recs = read_reasoning_tier_decisions(wiki, tier="T2")
        assert len(recs) == 1
        assert recs[0]["reason_code"] == SAFE_CLASS_VIOLATION_TOO_MANY_PAGES

    def test_pii_member_safe_class_violation_escalates(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        sources = [
            _write_source(tmp_path / "a.md", name="A", pii=True),
            _write_source(tmp_path / "b.md", name="B"),
        ]
        client = _approve_client()
        handled, _ = _screen(
            tmp_path, wiki_root=wiki, member_paths=sources, client=client
        )
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=sources,
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)
        from athenaeum.reasoning_tiers import read_reasoning_tier_decisions

        recs = read_reasoning_tier_decisions(wiki, tier="T2")
        assert recs[0]["reason_code"] == SAFE_CLASS_VIOLATION_PII_FLAGGED

    def test_axiom_member_safe_class_violation_escalates(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        sources = [
            _write_source(tmp_path / "a.md", name="A", memory_class="axiom"),
            _write_source(tmp_path / "b.md", name="B", memory_class="axiom"),
        ]
        client = _approve_client()
        handled, _ = _screen(
            tmp_path, wiki_root=wiki, member_paths=sources, client=client
        )
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=sources,
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)
        from athenaeum.reasoning_tiers import read_reasoning_tier_decisions

        recs = read_reasoning_tier_decisions(wiki, tier="T2")
        assert recs[0]["reason_code"] == SAFE_CLASS_VIOLATION_AXIOM_MEMBER

    def test_mixed_memory_class_safe_class_violation_escalates(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        sources = [
            _write_source(tmp_path / "a.md", name="A", memory_class="fact"),
            _write_source(tmp_path / "b.md", name="B", memory_class="guideline"),
        ]
        client = _approve_client()
        handled, _ = _screen(
            tmp_path, wiki_root=wiki, member_paths=sources, client=client
        )
        assert handled is False
        from athenaeum.pending_merges import write_pending_merge

        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="Merged Topic",
            sources=sources,
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        _assert_reached_human_queue_not_wiki(wiki)
        from athenaeum.reasoning_tiers import read_reasoning_tier_decisions

        recs = read_reasoning_tier_decisions(wiki, tier="T2")
        assert recs[0]["reason_code"] == SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS


# ---------------------------------------------------------------------------
# Non-approve verdicts (escalate/amend/draft) also fall through cleanly.
# ---------------------------------------------------------------------------


class TestNonApproveVerdictsFallThrough:
    def test_escalate_verdict_falls_through_to_human_queue(
        self, tmp_path: Path
    ) -> None:
        wiki = _wiki(tmp_path)
        client = _mock_client(
            '{"verdict": "escalate", "reason": "not confident", '
            '"amended_sources": null, "drafted_body": null}'
        )
        handled, _ = _screen(tmp_path, wiki_root=wiki, client=client)
        assert handled is False
        client.messages.create.assert_called_once()

    def test_amend_verdict_falls_through_to_human_queue(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        sources = _safe_pair(tmp_path)
        client = _mock_client(
            '{"verdict": "amend", "reason": "drop one source", '
            '"amended_sources": ["' + sources[0] + '"], "drafted_body": null}'
        )
        handled, _ = _screen(
            tmp_path, wiki_root=wiki, member_paths=sources, client=client
        )
        assert handled is False

    def test_draft_verdict_falls_through_to_human_queue(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        client = _mock_client(
            '{"verdict": "draft", "reason": "let a human review my draft", '
            '"amended_sources": null, "drafted_body": "## Rewritten body"}'
        )
        handled, _ = _screen(tmp_path, wiki_root=wiki, client=client)
        assert handled is False


# ---------------------------------------------------------------------------
# Guards mirroring T1's own: disabled / no-client / dry-run / no-members.
# ---------------------------------------------------------------------------


class TestT2ScreenGuards:
    def test_disabled_is_a_noop(self, tmp_path: Path) -> None:
        client = _approve_client()
        handled, _ = _screen(tmp_path, enabled=False, client=client)
        assert handled is False
        client.messages.create.assert_not_called()

    def test_no_client_is_a_noop(self, tmp_path: Path) -> None:
        handled, _ = _screen(tmp_path, client=None)
        assert handled is False

    def test_dry_run_is_a_noop(self, tmp_path: Path) -> None:
        client = _approve_client()
        handled, _ = _screen(tmp_path, dry_run=True, client=client)
        assert handled is False
        client.messages.create.assert_not_called()

    def test_empty_members_is_a_noop(self, tmp_path: Path) -> None:
        handled, _ = _screen(tmp_path, member_paths=[])
        assert handled is False


# ---------------------------------------------------------------------------
# Defense in depth — resolve_merge itself failing after the T2 approve
# (e.g. a slug collision snuck in between classification and finalize)
# must NOT be reported as a silent success; it must leave the block
# unresolved in the human queue, never partially-applied.
# ---------------------------------------------------------------------------


class TestResolveMergeFailureAfterApprove:
    def test_misclassified_create_for_existing_target_fails_closed_at_write(
        self, tmp_path: Path
    ) -> None:
        """Since athenaeum#748, a ``create-merged`` write_kind for a target slug
        that ALREADY exists is refused at WRITE time (an even stronger guard
        than the pre-existing approve-time ``target_exists`` fail-closed): the
        write is rejected, so nothing is queued, nothing is resolved, the
        pre-existing page is untouched, and no provenance is recorded.

        In production this mismatch is unreachable — the T2 path derives
        write_kind via ``_classify_merge_write_kind``, so an existing target
        yields ``fold-into-existing``, never ``create-merged``. Passing a stale
        ``create-merged`` here simulates that misclassification and pins the
        fail-closed behavior."""
        import pytest

        wiki = _wiki(tmp_path)
        (wiki / "merged-topic.md").write_text("pre-existing page\n", encoding="utf-8")
        pre_existing_mtime = (wiki / "merged-topic.md").stat().st_mtime

        client = _approve_client()
        with pytest.raises(ValueError, match="write_kind mismatch"):
            _screen(tmp_path, wiki_root=wiki, client=client)

        # Nothing was queued.
        merges_path = wiki / "_pending_merges.md"
        assert parse_pending_merges(merges_path) == [] or not merges_path.exists()
        # The pre-existing page must be untouched — no partial/garbled write.
        assert (wiki / "merged-topic.md").stat().st_mtime == pre_existing_mtime
        assert (wiki / "merged-topic.md").read_text(encoding="utf-8") == (
            "pre-existing page\n"
        )
        assert read_merge_provenance(wiki) == []
