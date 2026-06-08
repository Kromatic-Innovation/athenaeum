# SPDX-License-Identifier: Apache-2.0
"""Auto-apply prior HUMAN-ratified verdicts to matching new conflicts (#199).

Refines #198's blanket fingerprint suppression into three outcomes inside
``tier4_escalate``:

1. Cache hit with a HUMAN-ratified verdict -> AUTO-APPLY that verdict to the
   NEW conflict's source files (via #197's ``enact_resolution`` write-back),
   no new pending block, log ``"auto-applied prior human verdict <id>"``.
2. Cache hit with ONLY an auto verdict -> ESCALATE normally (never compound a
   prior automated mistake). This CHANGES #198's auto-suppression for the
   auto-only case.
3. No cache hit / fingerprint mismatch -> normal escalation (unchanged).

The happy-path test is fix-dependent: with auto-apply disabled the new source
member would survive AND a new pending block would be written.
"""

from __future__ import annotations

import logging
from pathlib import Path

from athenaeum.fingerprint import claim_pair_fingerprint, record_resolution
from athenaeum.models import EscalationItem
from athenaeum.tiers import tier4_escalate

CLAIM_A = "Acme is headquartered in Boston."
CLAIM_B = "Acme is headquartered in Austin."


def _knowledge_root(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _desc(p1: str, p2: str, members: tuple[str, str] | None = None) -> str:
    parts = ["Rationale text.", f"Passage 1: {p1}", f"Passage 2: {p2}"]
    if members:
        parts.append(f"Members involved: {members[0]}, {members[1]}")
    return "\n".join(parts)


def _source_member(root: Path, relname: str, body: str) -> Path:
    """Create a raw auto-memory member file and return its path."""
    p = root / "raw" / relname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {relname}\n---\n{body}\n", encoding="utf-8")
    return p


class TestAutoApplyHumanVerdict:
    def test_human_verdict_auto_applied_no_block_source_corrected(
        self, tmp_path: Path, caplog
    ) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        # A prior HUMAN resolution of the claim-pair: correct_a => side a is
        # right, side b's claim is wrong and gets deleted on enact.
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root,
            fingerprint=fp,
            verdict="correct_a",
            resolved_by="human",
            source_verdict_id="human-verdict-001",
        )

        # The NEW page carries the SAME claim-pair with REAL source members.
        member_a = _source_member(root, "pageY/a.md", CLAIM_B)
        member_b = _source_member(root, "pageY/b.md", CLAIM_A)
        item = EscalationItem(
            raw_ref="wiki/pageY.md",
            entity_name="AcmeOnPageY",
            conflict_type="factual",
            description=_desc(CLAIM_B, CLAIM_A, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )

        with caplog.at_level(logging.INFO, logger="athenaeum"):
            suppressed = tier4_escalate([item], pending)

        # (a) No new pending block for the already-adjudicated pair.
        assert not pending.exists() or "AcmeOnPageY" not in pending.read_text()
        assert suppressed == 1

        # (b) New source corrected: correct_a deletes side b (the wrong claim).
        assert not member_b.exists()
        assert member_a.exists()

        # (c) Audit log names the source verdict id.
        assert any(
            "auto-applied prior human verdict" in r.message
            and "human-verdict-001" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_auto_only_verdict_escalates_not_applied(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        # Only an AUTO verdict exists for the fingerprint -> never auto-apply.
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(root, fingerprint=fp, verdict="keep_a", resolved_by="auto")

        member_a = _source_member(root, "pageY/a.md", CLAIM_B)
        member_b = _source_member(root, "pageY/b.md", CLAIM_A)
        item = EscalationItem(
            raw_ref="wiki/pageY.md",
            entity_name="AutoOnlyPair",
            conflict_type="factual",
            description=_desc(CLAIM_B, CLAIM_A, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )
        suppressed = tier4_escalate([item], pending)

        # ESCALATES: a new pending block IS created; nothing auto-applied.
        assert pending.exists()
        assert "AutoOnlyPair" in pending.read_text()
        assert suppressed == 0
        # Source members untouched (no enactment on the auto-only path).
        assert member_a.exists()
        assert member_b.exists()

    def test_human_supersedes_prior_auto_for_same_fingerprint(
        self, tmp_path: Path
    ) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        # Auto recorded first, then a human ratifies the same fingerprint.
        record_resolution(root, fingerprint=fp, verdict="keep_a", resolved_by="auto")
        record_resolution(
            root,
            fingerprint=fp,
            verdict="correct_a",
            resolved_by="human",
            source_verdict_id="human-verdict-002",
        )

        member_a = _source_member(root, "pageZ/a.md", CLAIM_B)
        member_b = _source_member(root, "pageZ/b.md", CLAIM_A)
        item = EscalationItem(
            raw_ref="wiki/pageZ.md",
            entity_name="SupersededPair",
            conflict_type="factual",
            description=_desc(CLAIM_B, CLAIM_A, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )
        suppressed = tier4_escalate([item], pending)

        # Human ratification wins -> auto-apply (no block), source corrected.
        assert not pending.exists() or "SupersededPair" not in pending.read_text()
        assert suppressed == 1
        assert not member_b.exists()

    def test_material_change_escalates(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root,
            fingerprint=fp,
            verdict="correct_a",
            resolved_by="human",
            source_verdict_id="human-verdict-003",
        )

        # Materially different claim -> different fingerprint -> no cache hit.
        edited = "Acme is headquartered in Denver."
        member_a = _source_member(root, "pageQ/a.md", CLAIM_A)
        member_b = _source_member(root, "pageQ/b.md", edited)
        item = EscalationItem(
            raw_ref="wiki/pageQ.md",
            entity_name="MaterialChange",
            conflict_type="factual",
            description=_desc(CLAIM_A, edited, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        assert "MaterialChange" in pending.read_text()
        assert member_a.exists()
        assert member_b.exists()
