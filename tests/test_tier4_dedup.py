"""Tests for tier4 source-memory-pair dedup (issue #157).

`tier4_escalate` collapses escalations that share a source-memory pair
(``Members involved:`` tuple or, when absent, a passage-hash fallback)
into a single block annotated with ``**Also affects**: ...``. Archived
blocks are NOT in the open set, so a previously-resolved pair that
re-fires gets a fresh block (resurrection).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.answers import ingest_answers, parse_pending_questions
from athenaeum.models import EscalationItem
from athenaeum.tiers import (
    _append_also_affects,
    _pair_key_from_description,
    tier4_escalate,
)


def _desc_with_members(a: str, b: str, extra: str = "") -> str:
    """Build a description that mimics merge.py's contradictions output."""
    parts = []
    if extra:
        parts.append(extra)
    parts.append("Passage 1: foo")
    parts.append("Passage 2: bar")
    parts.append(f"Members involved: {a}, {b}")
    return "\n".join(parts)


def _desc_unsourced(p1: str, p2: str) -> str:
    """Description with passages but no Members involved line."""
    return f"Rationale text.\nPassage 1: {p1}\nPassage 2: {p2}"


def _item(entity: str, description: str, raw_ref: str = "wiki/x.md") -> EscalationItem:
    return EscalationItem(
        raw_ref=raw_ref,
        entity_name=entity,
        conflict_type="principled",
        description=description,
    )


# ---------------------------------------------------------------------------
# Pair-key helper
# ---------------------------------------------------------------------------


class TestPairKey:
    def test_members_order_independent(self) -> None:
        k1 = _pair_key_from_description(_desc_with_members("a.md", "b.md"))
        k2 = _pair_key_from_description(_desc_with_members("b.md", "a.md"))
        assert k1 == k2
        assert k1 == ("a.md", "b.md")

    def test_no_members_uses_passage_hash(self) -> None:
        d = _desc_unsourced("foo", "bar")
        k = _pair_key_from_description(d)
        assert k is not None
        assert k[0] == "__passage_hash__"

    def test_passage_hash_stable(self) -> None:
        k1 = _pair_key_from_description(_desc_unsourced("foo", "bar"))
        k2 = _pair_key_from_description(_desc_unsourced("foo", "bar"))
        assert k1 == k2

    def test_passage_hash_order_independent(self) -> None:
        # swapped passage order should collapse to the same key.
        k1 = _pair_key_from_description(_desc_unsourced("foo", "bar"))
        k2 = _pair_key_from_description(_desc_unsourced("bar", "foo"))
        assert k1 == k2

    def test_passage_hash_distinguishes_content(self) -> None:
        k1 = _pair_key_from_description(_desc_unsourced("foo", "bar"))
        k2 = _pair_key_from_description(_desc_unsourced("baz", "qux"))
        assert k1 != k2

    def test_returns_none_for_unkeyable(self) -> None:
        # Single member, single passage — cannot form a stable key.
        assert _pair_key_from_description("Members involved: solo.md") is None
        assert _pair_key_from_description("Passage 1: only") is None
        assert _pair_key_from_description("plain text only") is None


# ---------------------------------------------------------------------------
# Helper: _append_also_affects
# ---------------------------------------------------------------------------


class TestAppendAlsoAffects:
    def test_inserts_after_description(self) -> None:
        block = (
            '## [2026-05-23] Entity: "Alpha" (from wiki/x.md)\n'
            "- [ ] q?\n\n"
            "**Conflict type**: principled\n"
            "**Description**: line1\n"
        )
        out = _append_also_affects(block, "Beta")
        assert "**Also affects**: Beta" in out
        # Order: description before also affects.
        assert out.index("**Description**:") < out.index("**Also affects**:")

    def test_idempotent_for_same_entity(self) -> None:
        block = (
            '## [2026-05-23] Entity: "Alpha" (from wiki/x.md)\n'
            "- [ ] q?\n\n"
            "**Conflict type**: principled\n"
            "**Description**: d\n"
            "**Also affects**: Beta\n"
        )
        out = _append_also_affects(block, "Beta")
        # Only one occurrence of "Beta" on the also-affects line.
        affects_line = next(
            ln for ln in out.splitlines() if ln.startswith("**Also affects**:")
        )
        assert affects_line.count("Beta") == 1

    def test_skips_primary_entity(self) -> None:
        block = (
            '## [2026-05-23] Entity: "Alpha" (from wiki/x.md)\n'
            "- [ ] q?\n\n"
            "**Conflict type**: principled\n"
            "**Description**: d\n"
        )
        out = _append_also_affects(block, "Alpha")
        assert "**Also affects**" not in out

    def test_appends_to_existing_line(self) -> None:
        block = (
            '## [2026-05-23] Entity: "Alpha" (from wiki/x.md)\n'
            "- [ ] q?\n\n"
            "**Conflict type**: principled\n"
            "**Description**: d\n"
            "**Also affects**: Beta\n"
        )
        out = _append_also_affects(block, "Gamma")
        assert "**Also affects**: Beta, Gamma" in out


# ---------------------------------------------------------------------------
# tier4_escalate dedup behavior
# ---------------------------------------------------------------------------


class TestTier4Dedup:
    def test_dedup_happy_path(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        # One block, not two.
        assert content.count("## [") == 1
        assert "**Also affects**: Beta" in content
        # Primary entity stays Alpha.
        assert 'Entity: "Alpha"' in content
        assert 'Entity: "Beta"' not in content

    def test_no_dedup_for_distinct_pairs(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Gamma", _desc_with_members("c.md", "d.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert content.count("## [") == 2
        assert "**Also affects**" not in content

    def test_order_independent_pair_collapse(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("b.md", "a.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "**Also affects**: Beta" in content

    def test_triple_collapse(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
            _item("Gamma", _desc_with_members("a.md", "b.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "**Also affects**: Beta, Gamma" in content
        # Primary never repeats in the list.
        affects_line = next(
            ln for ln in content.splitlines() if ln.startswith("**Also affects**:")
        )
        assert "Alpha" not in affects_line

    def test_idempotent_same_entity_twice(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        affects_line = next(
            ln for ln in content.splitlines() if ln.startswith("**Also affects**:")
        )
        # Beta listed exactly once.
        assert affects_line.count("Beta") == 1

    def test_cross_batch_file_merge(self, tmp_path: Path) -> None:
        """Batch 2 with the same pair as an open block in the file merges in-place."""
        pending = tmp_path / "_pending_questions.md"
        tier4_escalate([_item("Alpha", _desc_with_members("a.md", "b.md"))], pending)
        # New batch — same pair, different entity.
        tier4_escalate([_item("Beta", _desc_with_members("a.md", "b.md"))], pending)
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "**Also affects**: Beta" in content

    def test_resurrection_creates_new_block(self, tmp_path: Path) -> None:
        """Once a block is archived ([x]), the same pair re-firing makes a NEW block."""
        pending = tmp_path / "_pending_questions.md"
        raw_root = tmp_path / "raw"

        tier4_escalate([_item("Alpha", _desc_with_members("a.md", "b.md"))], pending)
        # Flip the checkbox to [x] and let ingest archive the block.
        text = pending.read_text().replace("- [ ]", "- [x]", 1)
        # Add an answer body so the archive writer is happy.
        text = text.replace("- [x]", "- [x]\n\nresolved by hand\n", 1)
        pending.write_text(text)
        ingested = ingest_answers(pending, raw_root)
        assert ingested == 1
        # Open file should now be just the header.
        assert "## [" not in pending.read_text()

        # Same pair re-fires — must produce a fresh block.
        tier4_escalate([_item("Alpha", _desc_with_members("a.md", "b.md"))], pending)
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "**Also affects**" not in content

    def test_unsourced_fallback_dedup(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_unsourced("payload-X", "payload-Y")),
            _item("Beta", _desc_unsourced("payload-X", "payload-Y")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "**Also affects**: Beta" in content

    def test_unsourced_distinct_passages_no_dedup(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_unsourced("payload-X", "payload-Y")),
            _item("Beta", _desc_unsourced("other-A", "other-B")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert content.count("## [") == 2

    def test_disable_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_TIER4_DEDUP", "false")
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        # Disabled — two blocks, no Also affects line.
        assert content.count("## [") == 2
        assert "**Also affects**" not in content

    def test_deduped_block_round_trips_through_parser(self, tmp_path: Path) -> None:
        """The **Also affects**: line must NOT leak into answer_lines."""
        pending = tmp_path / "_pending_questions.md"
        items = [
            _item("Alpha", _desc_with_members("a.md", "b.md")),
            _item("Beta", _desc_with_members("a.md", "b.md")),
        ]
        tier4_escalate(items, pending)
        pqs = parse_pending_questions(pending)
        assert len(pqs) == 1
        pq = pqs[0]
        assert pq.answered is False
        assert pq.answer_lines == []
        assert pq.also_affects == ["Beta"]


# ---------------------------------------------------------------------------
# Auto-apply (#156) interaction
# ---------------------------------------------------------------------------


class _StubProposal:
    """Minimal proposal stand-in for the auto-apply gate."""

    def __init__(self, confidence: float = 0.95) -> None:
        self.confidence = confidence
        self.action = "prefer_left"
        self.rationale = "stub rationale"
        self.winning_uid = "uid_alpha"


class TestTier4DedupAutoApply:
    def test_also_affects_survives_auto_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub out apply_auto_resolution to keep the test independent of
        # the resolutions module's exact rendering — we only care that
        # the **Also affects** line survives the rewrite pass.
        from athenaeum import resolutions

        def fake_apply(block: str, proposal, model=None):  # noqa: ANN001
            # Flip checkbox + tack on a marker line — that's what the
            # real auto-apply does in spirit, and it's enough to exercise
            # the interaction with _append_also_affects.
            block = block.replace("- [ ]", "- [x]", 1)
            return block + "\n_auto-resolved_\n"

        monkeypatch.setattr(resolutions, "apply_auto_resolution", fake_apply)
        monkeypatch.setattr(resolutions, "resolve_auto_apply", lambda c: True)
        monkeypatch.setattr(resolutions, "resolve_auto_apply_threshold", lambda c: 0.5)

        pending = tmp_path / "_pending_questions.md"
        proposal = _StubProposal(confidence=0.95)
        a = _item("Alpha", _desc_with_members("a.md", "b.md"))
        a.proposal = proposal
        b = _item("Beta", _desc_with_members("a.md", "b.md"))
        b.proposal = proposal
        tier4_escalate([a, b], pending, config={})

        content = pending.read_text()
        assert "_auto-resolved_" in content
        assert "**Also affects**: Beta" in content
        # Survives parser.
        pqs = parse_pending_questions(pending)
        assert len(pqs) == 1
        assert pqs[0].also_affects == ["Beta"]
