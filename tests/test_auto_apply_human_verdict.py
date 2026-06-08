# SPDX-License-Identifier: Apache-2.0
"""Auto-apply prior HUMAN-ratified verdicts to matching new conflicts (#199).

Refines #198's blanket fingerprint suppression into outcomes inside
``tier4_escalate``:

1. Cache hit with a HUMAN-ratified ORIENTATION-AGNOSTIC or correctly-oriented
   enacting verdict -> AUTO-APPLY that verdict to the NEW conflict's source
   files (via #197's ``enact_resolution`` write-back), no new pending block,
   log ``"auto-applied prior human verdict <id>"``.
2. Cache hit with a SWAPPED orientation -> FLIP the action (correct_a<->correct_b,
   keep_a<->keep_b, forget_a<->forget_b) so the correct member is hit. The
   claim-pair fingerprint is order-INDEPENDENT but enacting verdicts are
   orientation-DEPENDENT; without the flip auto-apply would delete the
   ORIGINALLY-CORRECT claim (silent corpus corruption).
3. Cache hit with ONLY an auto verdict -> ESCALATE normally (never compound a
   prior automated mistake).
4. Cache hit whose enacting verdict CANNOT be safely applied (anchorless /
   orientation unresolvable / members missing or short) -> FAIL SAFE: escalate
   so a human handles it, never silently drop.
5. No cache hit / fingerprint mismatch -> normal escalation (unchanged).

The swapped-orientation test is FIX-DEPENDENT: against the pre-fix code (which
applies the stored action verbatim in the new member order) it would delete the
wrong member and the originally-correct claim would NOT survive.
"""

from __future__ import annotations

import logging
from pathlib import Path

from athenaeum.fingerprint import (
    claim_pair_fingerprint,
    normalize_side,
    record_resolution,
)
from athenaeum.models import EscalationItem
from athenaeum.tiers import tier4_escalate

# CLAIM_A is the CORRECT claim throughout; CLAIM_B is the wrong one. A human
# verdict of correct_a (side a is right) therefore means "CLAIM_A survives,
# CLAIM_B's member is deleted".
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


def _record_human(
    root: Path,
    verdict: str,
    *,
    source_verdict_id: str,
    side_a: str | None = CLAIM_A,
    side_b: str | None = CLAIM_B,
    conflict_type: str = "factual",
) -> str:
    """Record a HUMAN verdict with per-side anchors (verdict a/b orientation).

    side_a / side_b are the ORIGINAL-orientation claim texts; passing None
    omits the anchor (simulates a pre-#199 record).
    """
    fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, conflict_type)
    record_resolution(
        root,
        fingerprint=fp,
        verdict=verdict,
        resolved_by="human",
        source_verdict_id=source_verdict_id,
        side_a_norm=normalize_side(side_a) if side_a is not None else None,
        side_b_norm=normalize_side(side_b) if side_b is not None else None,
    )
    return fp


class TestAutoApplyHumanVerdict:
    def test_aligned_orientation_auto_applied_no_block(
        self, tmp_path: Path, caplog
    ) -> None:
        """Happy path: new conflict in the SAME a/b order as the verdict.

        correct_a with anchors (a=CLAIM_A correct). New conflict members are
        [a=CLAIM_A, b=CLAIM_B] (aligned). Auto-apply deletes side b (CLAIM_B),
        the CORRECT claim (CLAIM_A) survives, no new block.
        """
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(root, "correct_a", source_verdict_id="human-verdict-001")

        member_a = _source_member(root, "pageY/a.md", CLAIM_A)  # correct
        member_b = _source_member(root, "pageY/b.md", CLAIM_B)  # wrong
        item = EscalationItem(
            raw_ref="wiki/pageY.md",
            entity_name="AcmeOnPageY",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )

        with caplog.at_level(logging.INFO, logger="athenaeum"):
            suppressed = tier4_escalate([item], pending)

        assert not pending.exists() or "AcmeOnPageY" not in pending.read_text()
        assert suppressed == 1
        # correct_a deletes side b (wrong); the correct claim survives.
        assert member_a.exists()
        assert not member_b.exists()
        assert any(
            "auto-applied prior human verdict" in r.message
            and "human-verdict-001" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_swapped_orientation_flips_action_correct_claim_survives(
        self, tmp_path: Path, caplog
    ) -> None:
        """MUST #2 — swapped orientation must NOT corrupt the corpus.

        Human verdict correct_a (a=CLAIM_A correct). The new page surfaces the
        SAME pair but with members/passages in the OPPOSITE order:
        [a=CLAIM_B, b=CLAIM_A]. The order-independent fingerprint still matches.

        Pre-fix: applies correct_a verbatim -> deletes member_paths[1] (=CLAIM_A,
        the CORRECT claim) -> corpus corruption.
        Fixed: detects REVERSED orientation, flips to correct_b -> deletes
        member_paths[0] (=CLAIM_B, the wrong claim). CLAIM_A survives.
        """
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(root, "correct_a", source_verdict_id="human-verdict-swap")

        # OPPOSITE order: side a is now the WRONG claim, side b the correct one.
        member_a = _source_member(root, "pageS/a.md", CLAIM_B)  # wrong, side a
        member_b = _source_member(root, "pageS/b.md", CLAIM_A)  # correct, side b
        item = EscalationItem(
            raw_ref="wiki/pageS.md",
            entity_name="SwappedPair",
            conflict_type="factual",
            description=_desc(CLAIM_B, CLAIM_A, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )

        with caplog.at_level(logging.INFO, logger="athenaeum"):
            suppressed = tier4_escalate([item], pending)

        assert suppressed == 1
        assert not pending.exists() or "SwappedPair" not in pending.read_text()
        # THE INVARIANT: the originally-correct claim (CLAIM_A) SURVIVES.
        assert member_b.exists(), "originally-correct claim was deleted (corruption)"
        # The wrong claim (CLAIM_B) is the one deleted.
        assert not member_a.exists()
        # Log records the flip.
        assert any(
            "auto-applied prior human verdict" in r.message
            and "applied_action=correct_b" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_anchorless_enacting_verdict_escalates(self, tmp_path: Path) -> None:
        """Fail-safe #4: a pre-#199 record (no anchors) for an enacting verdict
        cannot have its orientation resolved -> escalate, never auto-delete."""
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(
            root,
            "correct_a",
            source_verdict_id="human-verdict-noanchor",
            side_a=None,
            side_b=None,
        )

        member_a = _source_member(root, "pageN/a.md", CLAIM_A)
        member_b = _source_member(root, "pageN/b.md", CLAIM_B)
        item = EscalationItem(
            raw_ref="wiki/pageN.md",
            entity_name="AnchorlessPair",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        assert pending.exists()
        assert "AnchorlessPair" in pending.read_text()
        # Nothing deleted — fail safe.
        assert member_a.exists()
        assert member_b.exists()

    def test_missing_members_enacting_verdict_escalates_no_autoapply_log(
        self, tmp_path: Path, caplog
    ) -> None:
        """SHOULD #3: enacting human cache-hit with NO members must escalate
        (block created) and emit NO "auto-applied" log line."""
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(root, "correct_a", source_verdict_id="human-verdict-nomembers")

        item = EscalationItem(
            raw_ref="wiki/pageM.md",
            entity_name="NoMembersPair",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B),  # no members in description
            members=[],  # missing members
        )
        with caplog.at_level(logging.INFO, logger="athenaeum"):
            suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        assert pending.exists()
        assert "NoMembersPair" in pending.read_text()
        # No "auto-applied" log because nothing was enacted.
        assert not any(
            "auto-applied prior human verdict" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_orientation_agnostic_verdict_suppresses_no_flip(
        self, tmp_path: Path
    ) -> None:
        """not_a_conflict (non-enacting, orientation-agnostic) suppresses on a
        cache hit regardless of member order — no flip, no block."""
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(
            root,
            "not_a_conflict",
            source_verdict_id="human-verdict-nac",
            side_a=CLAIM_A,
            side_b=CLAIM_B,
        )

        item = EscalationItem(
            raw_ref="wiki/pageA.md",
            entity_name="AgnosticPair",
            conflict_type="factual",
            description=_desc(CLAIM_B, CLAIM_A),  # swapped, but agnostic
            members=[],
        )
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 1
        assert not pending.exists() or "AgnosticPair" not in pending.read_text()

    def test_auto_only_verdict_escalates_not_applied(self, tmp_path: Path) -> None:
        """Auto-only cache hit never auto-applies (would compound a mistake)."""
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"

        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(
            root,
            fingerprint=fp,
            verdict="keep_a",
            resolved_by="auto",
            side_a_norm=normalize_side(CLAIM_A),
            side_b_norm=normalize_side(CLAIM_B),
        )

        member_a = _source_member(root, "pageAO/a.md", CLAIM_A)
        member_b = _source_member(root, "pageAO/b.md", CLAIM_B)
        item = EscalationItem(
            raw_ref="wiki/pageAO.md",
            entity_name="AutoOnlyPair",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        assert pending.exists()
        assert "AutoOnlyPair" in pending.read_text()
        assert member_a.exists()
        assert member_b.exists()

    def test_gate_variable_isolated_auto_vs_human(self, tmp_path: Path) -> None:
        """SHOULD #4: identical input, flip ONLY resolved_by. human -> auto-apply
        (no block); auto -> escalate (block). Isolates the gate variable."""

        def _run(resolved_by: str, tag: str) -> tuple[int, bool]:
            root = _knowledge_root(tmp_path / tag)
            pending = root / "wiki" / "_pending_questions.md"
            fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
            record_resolution(
                root,
                fingerprint=fp,
                verdict="correct_a",
                resolved_by=resolved_by,
                source_verdict_id="gate",
                side_a_norm=normalize_side(CLAIM_A),
                side_b_norm=normalize_side(CLAIM_B),
            )
            member_a = _source_member(root, "g/a.md", CLAIM_A)
            member_b = _source_member(root, "g/b.md", CLAIM_B)
            item = EscalationItem(
                raw_ref="wiki/g.md",
                entity_name="GatePair",
                conflict_type="factual",
                description=_desc(CLAIM_A, CLAIM_B, (str(member_a), str(member_b))),
                members=[str(member_a), str(member_b)],
            )
            suppressed = tier4_escalate([item], pending)
            block_created = pending.exists() and "GatePair" in pending.read_text()
            return suppressed, block_created

        human_suppressed, human_block = _run("human", "human")
        auto_suppressed, auto_block = _run("auto", "auto")

        # human: auto-applied, suppressed, no block.
        assert human_suppressed == 1 and not human_block
        # auto: escalated, not suppressed, block created.
        assert auto_suppressed == 0 and auto_block

    def test_failed_enact_escalates_no_autoapply_log(
        self, tmp_path: Path, caplog, monkeypatch
    ) -> None:
        """#203: enact_resolution returns None (file op failed / no-op) on an
        otherwise auto-appliable HUMAN verdict -> must ESCALATE (block created),
        emit NO "auto-applied" log, and NOT increment suppressed_count.

        Fix-dependent: pre-fix code ignores the return value, logs auto-applied
        and suppresses even though the source member was NOT corrected
        (stale-retain). Post-fix falls through to escalation.
        """
        import athenaeum.resolutions as resolutions_mod

        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(root, "correct_a", source_verdict_id="human-verdict-enactfail")

        member_a = _source_member(root, "pageF/a.md", CLAIM_A)  # correct
        member_b = _source_member(root, "pageF/b.md", CLAIM_B)  # wrong
        item = EscalationItem(
            raw_ref="wiki/pageF.md",
            entity_name="EnactFailPair",
            conflict_type="factual",
            description=_desc(CLAIM_A, CLAIM_B, (str(member_a), str(member_b))),
            members=[str(member_a), str(member_b)],
        )

        # Force enact to fail (simulates OSError on unlink/write or a no-op).
        monkeypatch.setattr(resolutions_mod, "enact_resolution", lambda *a, **k: None)

        with caplog.at_level(logging.INFO, logger="athenaeum"):
            suppressed = tier4_escalate([item], pending)

        # Escalates: not suppressed, block created.
        assert suppressed == 0
        assert pending.exists()
        assert "EnactFailPair" in pending.read_text()
        # No "auto-applied" log because nothing was actually enacted.
        assert not any(
            "auto-applied prior human verdict" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_material_change_escalates(self, tmp_path: Path) -> None:
        """Materially different claim -> different fingerprint -> no cache hit."""
        root = _knowledge_root(tmp_path)
        pending = root / "wiki" / "_pending_questions.md"
        _record_human(root, "correct_a", source_verdict_id="human-verdict-003")

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
