"""Tests for the resolved-contradiction fingerprint cache (issue #198).

The contradiction detector compares passage PAIRS per page, so an
already-adjudicated claim re-escalates as a brand-new pending question on
every new page that carries it. The fingerprint cache gives a settled
claim-pair a stable, page-independent hash and an append-only JSONL cache so
``tier4_escalate`` can SUPPRESS conflicts already resolved by a human or by
the auto-apply lane.

Acceptance test (end-to-end suppression) is ``TestEndToEndSuppression`` —
it is fix-dependent: with suppression disabled it would create a new block.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.answers import ingest_answers
from athenaeum.fingerprint import (
    claim_pair_fingerprint,
    fingerprint_from_description,
    is_resolved,
    load_resolved,
    record_resolution,
)
from athenaeum.models import EscalationItem
from athenaeum.tiers import tier4_escalate

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CLAIM_A = "Acme is headquartered in Boston."
CLAIM_B = "Acme is headquartered in Austin."


def _knowledge_root(tmp_path: Path) -> Path:
    """Create a <root>/wiki and <root>/raw layout and return <root>."""
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _desc(p1: str, p2: str, members: tuple[str, str] | None = None) -> str:
    parts = ["Rationale text.", f"Passage 1: {p1}", f"Passage 2: {p2}"]
    if members:
        parts.append(f"Members involved: {members[0]}, {members[1]}")
    return "\n".join(parts)


def _item(
    entity: str,
    description: str,
    conflict_type: str = "factual",
    raw_ref: str = "wiki/x.md",
) -> EscalationItem:
    return EscalationItem(
        raw_ref=raw_ref,
        entity_name=entity,
        conflict_type=conflict_type,
        description=description,
    )


# ---------------------------------------------------------------------------
# Fingerprint stability + invalidation
# ---------------------------------------------------------------------------


class TestFingerprintStability:
    def test_order_independent(self) -> None:
        f1 = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        f2 = claim_pair_fingerprint(CLAIM_B, CLAIM_A, "factual")
        assert f1 == f2

    def test_cosmetic_churn_stable(self) -> None:
        f1 = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        f2 = claim_pair_fingerprint(
            "  ACME   is   HEADQUARTERED in boston.  ",
            "acme IS headquartered in AUSTIN.",
            "factual",
        )
        assert f1 == f2

    def test_page_identity_independent(self) -> None:
        # The fingerprint depends only on claim texts + conflict_type, not on
        # which page/member surfaced them.
        d_page_x = _desc(CLAIM_A, CLAIM_B, ("pageX/a.md", "pageX/b.md"))
        d_page_y = _desc(CLAIM_B, CLAIM_A, ("pageY/c.md", "pageY/d.md"))
        assert fingerprint_from_description(
            d_page_x, "factual"
        ) == fingerprint_from_description(d_page_y, "factual")

    def test_material_change_invalidates(self) -> None:
        f1 = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        f2 = claim_pair_fingerprint(
            CLAIM_A, "Acme is headquartered in Denver.", "factual"
        )
        assert f1 != f2

    def test_conflict_type_distinguishes(self) -> None:
        f1 = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        f2 = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "prescriptive")
        assert f1 != f2

    def test_too_few_passages_returns_none(self) -> None:
        assert fingerprint_from_description("Passage 1: only", "factual") is None


# ---------------------------------------------------------------------------
# JSONL cache round-trip
# ---------------------------------------------------------------------------


class TestCacheRoundTrip:
    def test_record_and_load(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root,
            fingerprint=fp,
            verdict="correct_a",
            resolved_by="human",
            source_verdict_id="abc123",
        )
        assert is_resolved(root, fp)
        assert fp in load_resolved(root)

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        assert load_resolved(root) == set()
        assert not is_resolved(root, "deadbeef")

    def test_record_writes_resolved_by(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root, fingerprint=fp, verdict="auto-applied", resolved_by="auto"
        )
        cache = (root / "raw" / "_resolved_contradictions.jsonl").read_text()
        rec = json.loads(cache.splitlines()[0])
        assert rec["resolved_by"] == "auto"
        assert rec["fingerprint"] == fp


# ---------------------------------------------------------------------------
# Escalation embeds the fingerprint
# ---------------------------------------------------------------------------


class TestEscalationEmbedsFingerprint:
    def test_block_carries_fingerprint_line(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        item = _item("Acme", _desc(CLAIM_A, CLAIM_B), conflict_type="factual")
        tier4_escalate([item], pending)
        text = pending.read_text(encoding="utf-8")
        expected = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        assert f"**Fingerprint**: {expected}" in text


# ---------------------------------------------------------------------------
# End-to-end suppression (the acceptance test)
# ---------------------------------------------------------------------------


class TestEndToEndSuppression:
    def test_resolved_pair_suppressed_on_new_page(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        # Page X surfaces the conflict; record its resolution to the cache.
        # Use an ORIENTATION-AGNOSTIC verdict (not_a_conflict) so this stays a
        # pure-suppression test: #199's orientation reconciliation only gates
        # the orientation-DEPENDENT enacting verdicts (correct/keep/forget),
        # which — with no real source members — would correctly escalate
        # rather than silently suppress. not_a_conflict suppresses unchanged.
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root, fingerprint=fp, verdict="not_a_conflict", resolved_by="human"
        )

        # Page Y carries the SAME claim-pair (swapped order, different members).
        item = _item(
            "AcmeOnPageY",
            _desc(CLAIM_B, CLAIM_A, ("pageY/c.md", "pageY/d.md")),
            conflict_type="factual",
        )
        suppressed = tier4_escalate([item], pending)

        # No new pending block created for the already-adjudicated pair.
        assert not pending.exists() or "AcmeOnPageY" not in pending.read_text()
        assert suppressed == 1

    def test_unresolved_pair_still_escalates(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        item = _item("FreshConflict", _desc(CLAIM_A, CLAIM_B))
        suppressed = tier4_escalate([item], pending)
        assert suppressed == 0
        assert "FreshConflict" in pending.read_text()

    def test_material_change_reescalates(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        # Resolve the original pair.
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root, fingerprint=fp, verdict="correct_a", resolved_by="human"
        )

        # Page Y carries a MATERIALLY edited claim → new fingerprint → escalates.
        edited = "Acme is headquartered in Denver."
        item = _item("EditedClaim", _desc(CLAIM_A, edited), conflict_type="factual")
        suppressed = tier4_escalate([item], pending)
        assert suppressed == 0
        assert "EditedClaim" in pending.read_text()


# ---------------------------------------------------------------------------
# resolved_by recorded correctly on BOTH paths
# ---------------------------------------------------------------------------


def _resolved_records(root: Path) -> list[dict]:
    cache = root / "raw" / "_resolved_contradictions.jsonl"
    if not cache.exists():
        return []
    return [json.loads(ln) for ln in cache.read_text().splitlines() if ln.strip()]


class TestResolvedByRecorded:
    def test_human_path_records_human(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        # Escalate a fresh conflict (embeds the fingerprint line).
        item = _item("Acme", _desc(CLAIM_A, CLAIM_B), conflict_type="factual")
        tier4_escalate([item], pending)

        # Human flips the checkbox and answers, then ingest archives it.
        text = pending.read_text().replace("- [ ]", "- [x]", 1)
        text += "\ncorrect_a Boston is right.\n"
        pending.write_text(text, encoding="utf-8")

        ingest_answers(pending, root / "raw")

        recs = _resolved_records(root)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        human = [r for r in recs if r["fingerprint"] == fp]
        assert human, "expected a recorded resolution for the answered pair"
        assert all(r["resolved_by"] == "human" for r in human)

    def test_auto_path_records_auto(self, tmp_path: Path) -> None:
        from athenaeum.resolutions import ResolutionProposal

        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        item = EscalationItem(
            raw_ref="wiki/x.md",
            entity_name="Acme",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B, ("a.md", "b.md")),
            proposal=ResolutionProposal(
                recommended_winner="a",
                action="keep_a",
                rationale="a is sourced, b is not",
                confidence=0.99,
            ),
            members=["a.md", "b.md"],
        )
        cfg = {"resolve": {"auto_apply": True, "auto_apply_threshold": 0.90}}
        tier4_escalate([item], pending, config=cfg)

        recs = _resolved_records(root)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        auto = [r for r in recs if r["fingerprint"] == fp]
        assert auto, "expected a recorded resolution for the auto-applied pair"
        assert all(r["resolved_by"] == "auto" for r in auto)
